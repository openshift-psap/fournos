"""Reconciler e2e tests — verify cleanup of dangling Kueue Workloads.

Requires FOURNOS_RECONCILE_INTERVAL_SEC=10 (set by ``make dev-run``).
With interval=10s the minimum-age threshold is 20s, so tests wait ~30s
for the reconciler to act.
"""

from __future__ import annotations

import time

import httpx

from tests.conftest import (
    delete_pipelinerun,
    poll_job_status,
    submit_job,
    workload_exists,
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

    time.sleep(30)
    assert not workload_exists(job_id), (
        "Reconciler should have deleted the orphaned Workload"
    )


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

    # The reconciler should detect the stale Workload (finished PR, no
    # notify callback) and delete it within a few cycles.
    time.sleep(30)
    assert not workload_exists(job_id), (
        "Reconciler should have deleted the stale Workload"
    )
