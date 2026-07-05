"""Background K8s watcher that archives FournosJobs to PostgreSQL."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone

from dateutil.parser import parse as parse_dt
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import db, k8s_client
from app.config import settings

logger = logging.getLogger(__name__)

_watcher_engine = None
_watcher_session: async_sessionmaker | None = None

SYNC_INTERVAL_SECONDS = 60
TERMINAL_PHASES = {"Succeeded", "Failed", "Stopped"}


def _init_watcher_db(loop: asyncio.AbstractEventLoop) -> None:
    """Create a separate DB engine for the watcher's own event loop."""
    global _watcher_engine, _watcher_session
    _watcher_engine = create_async_engine(
        settings.database_url, echo=False, pool_size=3, max_overflow=5,
    )
    _watcher_session = async_sessionmaker(
        _watcher_engine, class_=AsyncSession, expire_on_commit=False,
    )


def _extract_forge_fields(job: dict) -> dict:
    """Pull project/preset/cluster/owner/mlflow fields from a FournosJob dict."""
    meta = job.get("metadata", {})
    spec = job.get("spec", {})
    forge = spec.get("executionEngine", {}).get("forge", {})
    status = job.get("status", {})

    mlflow_info = (
        status.get("engineStatus", {})
        .get("forge", {})
        .get("exportArtifacts", {})
        .get("caliper_artifacts_export", {})
        .get("backends", {})
        .get("mlflow", {})
    )
    mlflow_url = mlflow_info.get("run_url", "") if mlflow_info else ""

    args = forge.get("args", [])
    preset = " ".join(args) if args else ""

    created_str = meta.get("creationTimestamp", "")
    created_at = None
    if created_str:
        try:
            created_at = parse_dt(created_str)
        except Exception:
            pass

    completed_at = None
    duration_seconds = None
    conditions = status.get("conditions", [])
    for cond in conditions:
        if cond.get("type") == "PipelineRunReady" and cond.get("status") in ("True", "False"):
            try:
                completed_at = parse_dt(cond["lastTransitionTime"])
            except Exception:
                pass

    if created_at and completed_at:
        duration_seconds = (completed_at - created_at).total_seconds()

    labels = meta.get("labels", {})
    schedule_name = labels.get("fournos-launcher/schedule-name", "")
    trigger_type = labels.get("fournos-launcher/trigger-type", "manual")

    return {
        "name": meta.get("name", ""),
        "project": forge.get("project", ""),
        "preset": preset,
        "cluster": spec.get("cluster", ""),
        "pipeline": spec.get("pipeline", ""),
        "owner": spec.get("owner", ""),
        "status": status.get("phase", "Unknown"),
        "message": status.get("message", ""),
        "created_at": created_at,
        "completed_at": completed_at,
        "duration_seconds": duration_seconds,
        "mlflow_url": mlflow_url,
        "config_overrides": forge.get("configOverrides", {}),
        "fjob_spec": spec,
        "fjob_status": status,
        "triggered_by_schedule": schedule_name or None,
        "trigger_type": trigger_type,
    }


async def _archive_job(job: dict) -> None:
    """Archive a single FournosJob to PostgreSQL."""
    if _watcher_session is None:
        logger.warning("Watcher DB not initialised -- skipping archive")
        return
    fields = _extract_forge_fields(job)
    job_name = fields.get("name")
    if not job_name:
        logger.warning("Skipping FournosJob with missing name")
        return

    async with _watcher_session() as session:
        async with session.begin():
            db_job = await db.upsert_job(session, **fields)
            await db.add_job_event(
                session,
                job_id=db_job.id,
                phase=fields["status"],
                message=fields["message"],
            )

    logger.info("Archived FournosJob %s (phase=%s)", job_name, fields["status"])


async def _full_sync() -> None:
    """List all FournosJobs from K8s and upsert each into PostgreSQL.

    This is a safety-net that runs periodically so jobs are never lost
    even if individual watch events fail to process.
    """
    if _watcher_session is None:
        return

    try:
        all_jobs = k8s_client.list_fournos_jobs()
    except Exception as exc:
        logger.warning("Full sync: failed to list FournosJobs: %s", exc)
        return

    if not all_jobs:
        return

    synced = 0
    errors = 0
    for job in all_jobs:
        try:
            await _archive_job(job)
            synced += 1
        except Exception as exc:
            name = job.get("metadata", {}).get("name", "?")
            logger.warning("Full sync: failed to archive %s: %s", name, exc)
            errors += 1

    logger.info("Full sync complete: %d synced, %d errors (out of %d)", synced, errors, len(all_jobs))


def _run_watch_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Blocking watch loop that runs in a background thread."""
    asyncio.set_event_loop(loop)
    _init_watcher_db(loop)

    # Validate DB connectivity before starting the watch
    try:
        loop.run_until_complete(_validate_db())
    except Exception as exc:
        logger.error("Watcher DB validation failed: %s", exc)

    # Initial full sync to catch any jobs created before the watcher started
    try:
        loop.run_until_complete(_full_sync())
    except Exception as exc:
        logger.warning("Initial full sync failed: %s", exc)

    resource_version = ""
    last_sync = time.monotonic()

    while True:
        try:
            logger.info("Starting FournosJob watch (rv=%s)", resource_version or "latest")
            for event in k8s_client.watch_fournos_jobs(
                resource_version=resource_version,
                timeout=300,
            ):
                obj = event.get("object", {})
                rv = obj.get("metadata", {}).get("resourceVersion", "")
                if rv:
                    resource_version = rv

                event_type = event.get("type", "")
                if event_type in ("ADDED", "MODIFIED"):
                    try:
                        loop.run_until_complete(_archive_job(obj))
                    except Exception as exc:
                        name = obj.get("metadata", {}).get("name", "?")
                        logger.error(
                            "Failed to archive event for %s (type=%s): %s",
                            name, event_type, exc,
                        )
                elif event_type == "DELETED":
                    name = obj.get("metadata", {}).get("name", "")
                    logger.info("FournosJob %s deleted from cluster", name)

                # Periodic full sync as safety net
                if time.monotonic() - last_sync > SYNC_INTERVAL_SECONDS:
                    try:
                        loop.run_until_complete(_full_sync())
                    except Exception as exc:
                        logger.warning("Periodic sync failed: %s", exc)
                    last_sync = time.monotonic()

        except Exception as exc:
            logger.warning("Watch stream error (will restart in 5s): %s", exc)
            time.sleep(5)
            resource_version = ""

        # Run a full sync between watch reconnections
        try:
            loop.run_until_complete(_full_sync())
        except Exception as exc:
            logger.warning("Reconnect sync failed: %s", exc)
        last_sync = time.monotonic()


async def _validate_db() -> None:
    """Quick check that the watcher can talk to PostgreSQL."""
    if _watcher_session is None:
        raise RuntimeError("Session not initialised")
    async with _watcher_session() as session:
        await session.execute(db.select(db.func.count(db.Job.id)))
    logger.info("Watcher DB connection validated")


_watch_thread: threading.Thread | None = None


def start_watcher() -> None:
    """Start the background watcher thread (idempotent)."""
    global _watch_thread
    if _watch_thread is not None and _watch_thread.is_alive():
        return

    if not k8s_client.is_connected():
        logger.warning("K8s not connected -- watcher not started")
        return

    loop = asyncio.new_event_loop()
    _watch_thread = threading.Thread(
        target=_run_watch_loop,
        args=(loop,),
        daemon=True,
        name="fjob-watcher",
    )
    _watch_thread.start()
    logger.info("FournosJob watcher started")
