"""Shutdown tests — stopping and terminating jobs via spec.shutdown."""

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


def _set_shutdown(k8s, name: str, mode: str) -> None:
    """Patch a FournosJob to set spec.shutdown to the given mode."""
    k8s.patch_namespaced_custom_object(
        GROUP,
        VERSION,
        NAMESPACE,
        PLURAL,
        name,
        body={"spec": {"shutdown": mode}},
    )


# ---------------------------------------------------------------------------
# Stop (graceful — CancelledRunFinally, runs finally tasks)
# ---------------------------------------------------------------------------


def test_stop_pending_job(k8s):
    """Stopping a Pending job sets phase=Stopped and deletes the Workload."""
    create_job(
        k8s,
        "test-stop-pending",
        {
            "hardware": {"gpuType": "a100", "gpuCount": 100},
            "forge": {"project": "testproj/llmd", "args": ["cks", "internal-test"]},
        },
    )

    poll_phase(k8s, "test-stop-pending", terminal={Phase.PENDING}, timeout=15)
    assert workload_exists("test-stop-pending"), "Workload should exist while Pending"

    _set_shutdown(k8s, "test-stop-pending", "Stop")

    phase = poll_phase(
        k8s,
        "test-stop-pending",
        terminal={Phase.STOPPED},
        timeout=30,
    )
    assert phase == Phase.STOPPED, job_status_summary(k8s, "test-stop-pending")

    job = get_job(k8s, "test-stop-pending")
    assert job["status"]["message"] == "Job stopped by user"

    conditions = {c["type"]: c for c in job["status"].get("conditions", [])}
    assert "WorkloadAdmitted" in conditions
    assert conditions["WorkloadAdmitted"]["status"] == "False"
    assert conditions["WorkloadAdmitted"]["reason"] == "Stopped"

    poll_resource_gone(workload_exists, "test-stop-pending")


def test_stop_running_job(k8s):
    """Stopping a Running job transitions through Stopping, then Stopped."""
    create_job(
        k8s,
        "test-stop-running",
        {
            "cluster": "cluster-1",
            "forge": {"project": "testproj/llmd", "args": ["cks", "internal-test"]},
        },
    )

    poll_phase(k8s, "test-stop-running", terminal={Phase.RUNNING}, timeout=30)

    _set_shutdown(k8s, "test-stop-running", "Stop")

    # The job should first transition to Stopping while PipelineRun cleans up.
    poll_phase(
        k8s,
        "test-stop-running",
        terminal={Phase.STOPPING},
        timeout=30,
    )
    # Workload must still exist while Stopping (quota not released yet).
    assert workload_exists("test-stop-running"), (
        "Workload must stay alive during Stopping phase"
    )

    # Wait for the final Stopped phase.
    phase = poll_phase(
        k8s,
        "test-stop-running",
        terminal={Phase.STOPPED},
        timeout=60,
    )
    assert phase == Phase.STOPPED, job_status_summary(k8s, "test-stop-running")

    job = get_job(k8s, "test-stop-running")
    assert job["status"]["message"] == "Job stopped by user"

    conditions = {c["type"]: c for c in job["status"].get("conditions", [])}
    assert conditions["WorkloadAdmitted"]["status"] == "False"
    assert conditions["WorkloadAdmitted"]["reason"] == "Stopped"
    assert "PipelineRunReady" in conditions
    assert conditions["PipelineRunReady"]["status"] == "False"
    assert conditions["PipelineRunReady"]["reason"] == "Stopped"

    # PipelineRun should have been cancelled via CancelledRunFinally.
    pr = get_k8s_resource("pipelinerun", "test-stop-running")
    assert pr["spec"].get("status") == "CancelledRunFinally", (
        f"PipelineRun spec.status should be CancelledRunFinally, "
        f"got {pr['spec'].get('status')!r}"
    )

    # PipelineRun must have completed (completionTime set).
    assert pr.get("status", {}).get("completionTime"), (
        "PipelineRun should have completionTime set after stop completes"
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

    # Workload should be gone now that we're in Stopped.
    poll_resource_gone(workload_exists, "test-stop-running")


def test_stop_at_creation(k8s):
    """Creating a job with shutdown=Stop immediately sets phase=Stopped."""
    create_job(
        k8s,
        "test-stop-create",
        {
            "cluster": "cluster-1",
            "forge": {"project": "testproj/llmd", "args": ["cks", "internal-test"]},
            "shutdown": "Stop",
        },
    )

    phase = poll_phase(
        k8s,
        "test-stop-create",
        terminal={Phase.STOPPED},
        timeout=15,
    )
    assert phase == Phase.STOPPED, job_status_summary(k8s, "test-stop-create")

    job = get_job(k8s, "test-stop-create")
    assert job["status"]["message"] == "Job stopped by user"
    assert not workload_exists("test-stop-create"), (
        "No Workload should be created for a shutdown job"
    )


def test_stop_completed_job_is_noop(k8s):
    """Setting shutdown=Stop on a Succeeded job does not change its phase."""
    create_job(
        k8s,
        "test-stop-done",
        {
            "cluster": "cluster-1",
            "forge": {"project": "testproj/llmd", "args": ["cks", "internal-test"]},
        },
    )

    phase = poll_phase(
        k8s,
        "test-stop-done",
        terminal={Phase.SUCCEEDED, Phase.FAILED},
        timeout=60,
    )
    assert phase == Phase.SUCCEEDED, job_status_summary(k8s, "test-stop-done")

    job_before = get_job(k8s, "test-stop-done")

    _set_shutdown(k8s, "test-stop-done", "Stop")

    time.sleep(10)

    job_after = get_job(k8s, "test-stop-done")
    assert job_after["status"]["phase"] == Phase.SUCCEEDED, (
        f"Phase should remain Succeeded after stopping a completed job, "
        f"got {job_after['status']['phase']!r}"
    )
    assert job_after["status"]["message"] == job_before["status"]["message"], (
        "Message should not change after stopping a completed job"
    )


# ---------------------------------------------------------------------------
# Terminate (immediate — Cancelled, skips finally tasks)
# ---------------------------------------------------------------------------


def test_terminate_running_job(k8s):
    """Terminating a Running job uses Cancelled and transitions to Stopped."""
    create_job(
        k8s,
        "test-term-running",
        {
            "cluster": "cluster-1",
            "forge": {"project": "testproj/llmd", "args": ["cks", "internal-test"]},
        },
    )

    poll_phase(k8s, "test-term-running", terminal={Phase.RUNNING}, timeout=30)

    _set_shutdown(k8s, "test-term-running", "Terminate")

    poll_phase(
        k8s,
        "test-term-running",
        terminal={Phase.STOPPING},
        timeout=30,
    )
    assert workload_exists("test-term-running"), (
        "Workload must stay alive during Stopping phase"
    )

    phase = poll_phase(
        k8s,
        "test-term-running",
        terminal={Phase.STOPPED},
        timeout=60,
    )
    assert phase == Phase.STOPPED, job_status_summary(k8s, "test-term-running")

    job = get_job(k8s, "test-term-running")
    assert job["status"]["message"] == "Job stopped by user"

    conditions = {c["type"]: c for c in job["status"].get("conditions", [])}
    assert conditions["WorkloadAdmitted"]["status"] == "False"
    assert conditions["WorkloadAdmitted"]["reason"] == "Stopped"
    assert conditions["PipelineRunReady"]["status"] == "False"
    assert conditions["PipelineRunReady"]["reason"] == "Stopped"

    # PipelineRun should have been terminated via Cancelled (not CancelledRunFinally).
    pr = get_k8s_resource("pipelinerun", "test-term-running")
    assert pr["spec"].get("status") == "Cancelled", (
        f"PipelineRun spec.status should be Cancelled, got {pr['spec'].get('status')!r}"
    )

    assert pr.get("status", {}).get("completionTime"), (
        "PipelineRun should have completionTime set after terminate completes"
    )

    poll_resource_gone(workload_exists, "test-term-running")
