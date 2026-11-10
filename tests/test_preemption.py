"""End-to-end preemption handoff through Kueue, Fourno, and mock Tekton."""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime

import pytest

from fournos.core.constants import Phase
from tests.conftest import (
    NAMESPACE,
    create_job,
    get_job,
    get_job_phase,
    get_k8s_resource,
    job_status_summary,
    pipelinerun_exists,
    poll_phase,
    poll_resource_gone,
    workload_exists,
)

LOW = "test-preempt-low"
HIGH = "test-preempt-high"
MOCK_SLEEP_SECONDS = "90"


def _set_mock_sleep(seconds: str) -> None:
    subprocess.run(
        [
            "kubectl",
            "patch",
            "configmap",
            "fournos-mock-config",
            "-n",
            NAMESPACE,
            "-p",
            json.dumps({"data": {"sleep": seconds}}),
        ],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def slow_mock_pipeline():
    """Keep the low-priority mock run active until Kueue preempts it."""
    result = subprocess.run(
        [
            "kubectl",
            "get",
            "configmap",
            "fournos-mock-config",
            "-n",
            NAMESPACE,
            "-o",
            "jsonpath={.data.sleep}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    original_sleep = result.stdout.strip() or "3"
    _set_mock_sleep(MOCK_SLEEP_SECONDS)
    try:
        yield
    finally:
        _set_mock_sleep(original_sleep)


def _job_spec(priority: str) -> dict:
    return {
        "cluster": "cluster-1",
        "exclusive": True,
        "priority": priority,
        "executionEngine": {
            "forge": {
                "project": "testproj/llmd",
                "args": ["cks", "internal-test"],
            }
        },
    }


def _timestamp(raw: str) -> datetime:
    return datetime.fromisoformat(raw)


def test_higher_priority_job_waits_for_evicted_run_cleanup(
    k8s, slow_mock_pipeline
) -> None:
    create_job(k8s, LOW, _job_spec("adhoc"))
    poll_phase(k8s, LOW, terminal={Phase.RUNNING}, interval=1, timeout=90)
    assert workload_exists(LOW)

    create_job(k8s, HIGH, _job_spec("manual"))
    poll_phase(k8s, HIGH, terminal={Phase.PENDING}, interval=0.5, timeout=60)
    poll_phase(k8s, LOW, terminal={Phase.STOPPING}, interval=0.5, timeout=90)

    # Kueue must hold the victim's quota until its finally task succeeds.
    # Read the victim's phase on both sides of the replacement check to avoid
    # a false failure if the phase changes during the snapshot.
    saw_stopping_with_reservation = False
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        low_before = get_job_phase(k8s, LOW)
        if low_before == Phase.STOPPING:
            high_phase = get_job_phase(k8s, HIGH)
            victim_reserved = workload_exists(LOW)
            replacement_started = pipelinerun_exists(HIGH)
            low_after = get_job_phase(k8s, LOW)
            if low_after == Phase.STOPPING and victim_reserved:
                saw_stopping_with_reservation = True
                assert high_phase == Phase.PENDING, job_status_summary(k8s, HIGH)
                assert not replacement_started, (
                    "Replacement PipelineRun started before victim cleanup"
                )
        else:
            low_after = low_before
        if low_after == Phase.STOPPED:
            break
        time.sleep(0.5)
    else:
        pytest.fail(f"Victim did not stop: {job_status_summary(k8s, LOW)}")

    assert saw_stopping_with_reservation, (
        "Expected to observe the victim holding quota in Stopping"
    )
    low_status = get_job(k8s, LOW)["status"]
    assert low_status["phase"] == Phase.STOPPED
    assert low_status["stopReason"] == "Preempted"
    assert "preempt" in low_status["message"].lower()
    poll_resource_gone(workload_exists, LOW)

    low_pr = get_k8s_resource("pipelinerun", LOW)
    assert low_pr["spec"].get("status") == "CancelledRunFinally"
    cleanup_refs = [
        ref
        for ref in low_pr.get("status", {}).get("childReferences", [])
        if ref.get("pipelineTaskName") == "cleanup" and ref.get("kind") == "TaskRun"
    ]
    assert cleanup_refs, "Cancelled PipelineRun did not start its cleanup TaskRun"
    cleanup_run = get_k8s_resource("taskrun", cleanup_refs[0]["name"])
    cleanup_status = cleanup_run.get("status", {})
    assert cleanup_status.get("completionTime")
    assert any(
        condition.get("type") == "Succeeded" and condition.get("status") == "True"
        for condition in cleanup_status.get("conditions", [])
    ), "Cleanup TaskRun did not succeed"
    assert any(
        result.get("name") == "preemption-cleanup-verified"
        and result.get("value") == "true"
        for result in cleanup_status.get("results", [])
    ), "Cleanup TaskRun did not verify remote cleanup"

    poll_phase(k8s, HIGH, terminal={Phase.RUNNING}, interval=1, timeout=90)
    high_pr = get_k8s_resource("pipelinerun", HIGH)
    assert _timestamp(high_pr["metadata"]["creationTimestamp"]) >= _timestamp(
        cleanup_status["completionTime"]
    ), "Replacement PipelineRun was created before victim cleanup completed"
