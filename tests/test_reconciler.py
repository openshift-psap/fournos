"""Reconciler e2e tests — verify cleanup of dangling Kueue Workloads.

Requires FOURNOS_RECONCILE_INTERVAL_SEC to be set (``make dev-run`` uses 10).
The reconciler's min-age threshold is 2 × interval; the polling helper
below waits up to 3 × interval + margin so the test adapts automatically.
"""

from __future__ import annotations

import os
import time

import httpx

from tests.conftest import (
    delete_pipelinerun,
    poll_job_status,
    submit_job,
    workload_exists,
)

_RECONCILE_INTERVAL = float(os.environ.get("FOURNOS_RECONCILE_INTERVAL_SEC", "60"))


def _wait_until_workload_gone(job_id: str, *, reason: str) -> None:
    """Poll until the Workload is deleted, or fail with a clear message."""
    timeout = 3 * _RECONCILE_INTERVAL + 10
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not workload_exists(job_id):
            return
        time.sleep(2)
    raise AssertionError(
        f"Reconciler should have deleted the {reason} Workload "
        f"within {timeout:.0f}s (interval={_RECONCILE_INTERVAL:.0f}s)"
    )


def test_orphaned_workload_cleaned_up(client: httpx.Client):
    """Admitted Workload with no PipelineRun is deleted by the reconciler."""
    data = submit_job(
        client,
        {
            "name": "test-reconciler-orphan",
            "hardware": {"gpu_type": "a100", "gpu_count": 2},
            "forge": {"project": "testproj/llmd", "preset": "cks"},
            "priority": "nightly",
        },
    )
    job_id = data["id"]
    poll_job_status(client, job_id, timeout=30)

    delete_pipelinerun(job_id)
    assert workload_exists(job_id), (
        "Workload should still exist right after deleting PipelineRun"
    )

    _wait_until_workload_gone(job_id, reason="orphaned")


def test_stale_workload_cleaned_up(client: httpx.Client):
    """Finished PipelineRun with leftover Workload is cleaned up by reconciler."""
    data = submit_job(
        client,
        {
            "name": "test-reconciler-stale",
            "pipeline": "fournos-run-no-notify",
            "hardware": {"gpu_type": "a100", "gpu_count": 2},
            "forge": {"project": "testproj/llmd", "preset": "cks"},
            "priority": "nightly",
        },
    )
    job_id = data["id"]

    # Wait for running — at this point the Workload is admitted and the
    # PipelineRun exists but hasn't finished, so the reconciler won't touch it.
    poll_job_status(client, job_id, terminal={"running"}, timeout=60)
    assert workload_exists(job_id), (
        "Workload should exist while PipelineRun is still running"
    )

    # Now wait for the PipelineRun to finish.
    status = poll_job_status(
        client,
        job_id,
        terminal={"succeeded", "failed"},
        timeout=120,
    )
    assert status in ("succeeded", "failed")

    _wait_until_workload_gone(job_id, reason="stale")
