"""Abort tests — aborting jobs in various phases via spec.aborted."""

import time

from fournos.core.constants import Phase
from tests.conftest import (
    GROUP,
    NAMESPACE,
    PLURAL,
    VERSION,
    create_job,
    get_job,
    get_k8s_resource,
    job_status_summary,
    poll_phase,
    poll_resource_gone,
    workload_exists,
)


def _set_aborted(k8s, name: str) -> None:
    """Patch a FournosJob to set spec.aborted=true."""
    k8s.patch_namespaced_custom_object(
        GROUP,
        VERSION,
        NAMESPACE,
        PLURAL,
        name,
        body={"spec": {"aborted": True}},
    )


def test_abort_pending_job(k8s):
    """Aborting a Pending job sets phase=Aborted and deletes the Workload."""
    create_job(
        k8s,
        "test-abort-pending",
        {
            "hardware": {"gpuType": "a100", "gpuCount": 100},
            "forge": {"project": "testproj/llmd", "args": ["cks", "internal-test"]},
        },
    )

    poll_phase(k8s, "test-abort-pending", terminal={Phase.PENDING}, timeout=15)
    assert workload_exists("test-abort-pending"), "Workload should exist while Pending"

    _set_aborted(k8s, "test-abort-pending")

    phase = poll_phase(
        k8s,
        "test-abort-pending",
        terminal={Phase.ABORTED},
        timeout=30,
    )
    assert phase == Phase.ABORTED, job_status_summary(k8s, "test-abort-pending")

    job = get_job(k8s, "test-abort-pending")
    assert job["status"]["message"] == "Job aborted by user"

    conditions = {c["type"]: c for c in job["status"].get("conditions", [])}
    assert "WorkloadAdmitted" in conditions
    assert conditions["WorkloadAdmitted"]["status"] == "False"
    assert conditions["WorkloadAdmitted"]["reason"] == "Aborted"

    poll_resource_gone(workload_exists, "test-abort-pending")


def test_abort_running_job(k8s):
    """Aborting a Running job transitions through Aborting, then Aborted."""
    create_job(
        k8s,
        "test-abort-running",
        {
            "cluster": "cluster-1",
            "forge": {"project": "testproj/llmd", "args": ["cks", "internal-test"]},
        },
    )

    poll_phase(k8s, "test-abort-running", terminal={Phase.RUNNING}, timeout=30)

    _set_aborted(k8s, "test-abort-running")

    # The job should first transition to Aborting while PipelineRun cleans up.
    poll_phase(
        k8s,
        "test-abort-running",
        terminal={Phase.ABORTING},
        timeout=30,
    )
    # Workload must still exist while Aborting (quota not released yet).
    assert workload_exists("test-abort-running"), (
        "Workload must stay alive during Aborting phase"
    )

    # Wait for the final Aborted phase.
    phase = poll_phase(
        k8s,
        "test-abort-running",
        terminal={Phase.ABORTED},
        timeout=60,
    )
    assert phase == Phase.ABORTED, job_status_summary(k8s, "test-abort-running")

    job = get_job(k8s, "test-abort-running")
    assert job["status"]["message"] == "Job aborted by user"

    conditions = {c["type"]: c for c in job["status"].get("conditions", [])}
    assert conditions["WorkloadAdmitted"]["status"] == "False"
    assert conditions["WorkloadAdmitted"]["reason"] == "Aborted"
    assert "PipelineRunReady" in conditions
    assert conditions["PipelineRunReady"]["status"] == "False"
    assert conditions["PipelineRunReady"]["reason"] == "Aborted"

    # PipelineRun should have been cancelled via CancelledRunFinally.
    pr = get_k8s_resource("pipelinerun", "test-abort-running")
    assert pr["spec"].get("status") == "CancelledRunFinally", (
        f"PipelineRun spec.status should be CancelledRunFinally, "
        f"got {pr['spec'].get('status')!r}"
    )

    # PipelineRun must have completed (completionTime set).
    assert pr.get("status", {}).get("completionTime"), (
        "PipelineRun should have completionTime set after abort completes"
    )

    pr_conditions = pr.get("status", {}).get("conditions", [])
    assert pr_conditions, "PipelineRun should have conditions after completion"
    last_cond = pr_conditions[-1]
    assert last_cond["status"] == "False", (
        f"Cancelled PipelineRun condition status should be False, got {last_cond}"
    )
    assert (
        "cancel" in last_cond.get("message", "").lower()
        or "cancel" in last_cond.get("reason", "").lower()
    ), f"PipelineRun condition should mention cancellation, got {last_cond}"

    # Workload should be gone now that we're in Aborted.
    poll_resource_gone(workload_exists, "test-abort-running")


def test_abort_at_creation(k8s):
    """Creating a job with aborted=true immediately sets phase=Aborted."""
    create_job(
        k8s,
        "test-abort-create",
        {
            "cluster": "cluster-1",
            "forge": {"project": "testproj/llmd", "args": ["cks", "internal-test"]},
            "aborted": True,
        },
    )

    phase = poll_phase(
        k8s,
        "test-abort-create",
        terminal={Phase.ABORTED},
        timeout=15,
    )
    assert phase == Phase.ABORTED, job_status_summary(k8s, "test-abort-create")

    job = get_job(k8s, "test-abort-create")
    assert job["status"]["message"] == "Job aborted by user"
    assert not workload_exists("test-abort-create"), (
        "No Workload should be created for a pre-aborted job"
    )


def test_abort_completed_job_is_noop(k8s):
    """Setting aborted=true on a Succeeded job does not change its phase."""
    create_job(
        k8s,
        "test-abort-done",
        {
            "cluster": "cluster-1",
            "forge": {"project": "testproj/llmd", "args": ["cks", "internal-test"]},
        },
    )

    phase = poll_phase(
        k8s,
        "test-abort-done",
        terminal={Phase.SUCCEEDED, Phase.FAILED},
        timeout=60,
    )
    assert phase == Phase.SUCCEEDED, job_status_summary(k8s, "test-abort-done")

    job_before = get_job(k8s, "test-abort-done")

    _set_aborted(k8s, "test-abort-done")

    time.sleep(10)

    job_after = get_job(k8s, "test-abort-done")
    assert job_after["status"]["phase"] == Phase.SUCCEEDED, (
        f"Phase should remain Succeeded after aborting a completed job, "
        f"got {job_after['status']['phase']!r}"
    )
    assert job_after["status"]["message"] == job_before["status"]["message"], (
        "Message should not change after aborting a completed job"
    )
