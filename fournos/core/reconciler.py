"""Periodic reconciler that cleans up dangling Kueue Workloads.

Handles two cases where a Workload leaks quota:

1. **Orphaned** — admitted Workload with no matching PipelineRun (the
   fire-and-forget admission-polling task was lost, e.g. process restart).
2. **Stale** — Workload whose PipelineRun has already finished but the
   ``fournos-notify`` completion callback failed to delete it.

Both cases are guarded by a minimum age threshold (2 x reconcile interval)
to avoid racing with the normal fast-path.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from fournos.core.kueue import KueueClient
from fournos.core.tekton import TektonClient

logger = logging.getLogger(__name__)

_TERMINAL_PR_STATUSES = {"succeeded", "failed"}


def _admission_age_seconds(wl: dict) -> float:
    """Return seconds since the Workload was admitted, or 0 if unknown."""
    for c in wl.get("status", {}).get("conditions", []):
        if c.get("type") == "Admitted" and c.get("status") == "True":
            ts = c.get("lastTransitionTime")
            if ts:
                admitted_at = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                return (datetime.now(timezone.utc) - admitted_at).total_seconds()
    return 0.0


async def reconcile_once(
    kueue: KueueClient,
    tekton: TektonClient,
    min_age: float,
) -> None:
    """Delete Workloads that are orphaned or whose PipelineRun has finished.

    Only acts on Workloads admitted for longer than *min_age* seconds, giving
    the normal fire-and-forget and completion-callback paths time to run.
    """
    logger.info("Reconciler: scanning for dangling Workloads")
    workloads = await asyncio.to_thread(kueue.list_workloads)
    pipeline_runs = await asyncio.to_thread(tekton.list_pipeline_runs)

    pr_by_job_id: dict[str, dict] = {}
    for pr in pipeline_runs:
        job_id = pr["metadata"].get("labels", {}).get("fournos.dev/job-id", "")
        if job_id:
            pr_by_job_id[job_id] = pr

    for wl in workloads:
        labels = wl["metadata"].get("labels", {})
        job_id = labels.get("fournos.dev/job-id", "")
        if not job_id:
            continue

        if not KueueClient.is_admitted(wl):
            continue

        age = _admission_age_seconds(wl)
        if age < min_age:
            continue

        pr = pr_by_job_id.get(job_id)

        if pr is None:
            reason = "admitted %.0fs ago, no PipelineRun" % age
        elif TektonClient.extract_status(pr) in _TERMINAL_PR_STATUSES:
            reason = "admitted %.0fs ago, PipelineRun %s" % (
                age,
                TektonClient.extract_status(pr),
            )
        else:
            continue

        logger.warning(
            "Reconciler: deleting dangling Workload for job %s (%s)",
            job_id,
            reason,
        )
        try:
            await asyncio.to_thread(kueue.delete_workload, job_id)
        except Exception:
            logger.exception("Reconciler: failed to delete Workload for job %s", job_id)


async def run_reconciler(
    kueue: KueueClient,
    tekton: TektonClient,
    interval: float,
) -> None:
    """Run the reconciliation loop until cancelled."""
    min_age = interval * 2
    while True:
        await asyncio.sleep(interval)
        try:
            await reconcile_once(kueue, tekton, min_age)
        except Exception:
            logger.exception("Reconciler iteration failed")
