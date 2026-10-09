"""Lifecycle handlers — on_create, reconcile_scheduled, reconcile_recurring,
and reconcile_pending.

Covers the early phases of a FournosJob: creation, optional deferred
scheduling, recurring cron-based scheduling, and pending admission
through Kueue.  Exclusive locking is handled entirely by Kueue via
cluster-slot resources.
"""

from __future__ import annotations

import copy
import logging
from datetime import UTC, datetime

from croniter import croniter
from kubernetes import client as k8s_client

from fournos.core.constants import (
    ANNOTATION_TRIGGER_NOW,
    CLUSTER_SLOT_RESOURCE,
    LABEL_EXCLUSIVE_CLUSTER,
    LABEL_RECURRING_PARENT,
    LOCK_HOLDING_PHASES,
    Phase,
)
from fournos.core.duration import parse_duration
from fournos.core.kueue import KueueClient
from fournos.settings import settings
from fournos.state import ctx

from .status import (
    COND_WORKLOAD_ADMITTED,
    CRD_GROUP,
    CRD_VERSION,
    owner_ref,
    set_condition,
    set_terminal_phase,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CREATE / RESUME handler
# ---------------------------------------------------------------------------


def on_create(spec, name, namespace, status, patch, body):
    if status.get("phase"):
        return

    shutdown = spec.get("shutdown")
    if shutdown is not None:
        set_terminal_phase(patch, Phase.STOPPED, "Job stopped by user")
        logger.info("Job %s: created with shutdown=%s, skipping", name, shutdown)
        return

    ttl_raw = spec.get("ttl")
    if ttl_raw and parse_duration(ttl_raw) is None:
        set_terminal_phase(patch, Phase.FAILED, f"Invalid ttl value: {ttl_raw!r}")
        logger.error("Job %s: invalid ttl %r", name, ttl_raw)
        return

    cron_expr = spec.get("schedule")
    try:
        scheduled_time = _parse_scheduled_time(spec)
    except ValueError as exc:
        set_terminal_phase(patch, Phase.FAILED, str(exc))
        return

    if cron_expr and scheduled_time is not None:
        set_terminal_phase(
            patch,
            Phase.FAILED,
            "'schedule' and 'scheduledStartTime' are mutually exclusive",
        )
        return

    if cron_expr:
        if not croniter.is_valid(cron_expr):
            set_terminal_phase(
                patch, Phase.FAILED, f"Invalid cron expression: {cron_expr}"
            )
            return
        patch.status["phase"] = Phase.RECURRING
        patch.status["message"] = f"Recurring schedule: {cron_expr}"
        logger.info("Job %s: phase=Recurring, schedule=%s", name, cron_expr)
        return

    if scheduled_time is not None and datetime.now(UTC) < scheduled_time:
        patch.status["phase"] = Phase.SCHEDULED
        patch.status["message"] = f"Scheduled to start at {scheduled_time.isoformat()}"
        logger.info("Job %s: phase=Scheduled, startTime=%s", name, scheduled_time)
        return

    cluster = spec.get("cluster")
    exclusive = spec["exclusive"]
    lock_only = is_lock_only(spec)
    clusterless = spec.get("clusterless", False)

    if lock_only and not cluster:
        set_terminal_phase(
            patch, Phase.FAILED, "lockOnly: true requires 'cluster' to be set"
        )
        return

    if spec.get("lockUntil") and not lock_only:
        # lock_only is only False here if the user explicitly wrote
        # lockOnly: false — a bare lockUntil already implies lockOnly: true.
        set_terminal_phase(
            patch, Phase.FAILED, "lockUntil cannot be combined with lockOnly: false"
        )
        return

    try:
        parse_iso_timestamp(spec.get("lockUntil"), "lockUntil")
    except ValueError as exc:
        set_terminal_phase(patch, Phase.FAILED, str(exc))
        return

    if not lock_only and not spec.get("executionEngine"):
        set_terminal_phase(
            patch,
            Phase.FAILED,
            "spec.executionEngine is required for non-lockOnly jobs",
        )
        return

    if clusterless:
        for cond, msg in [
            (lock_only, "clusterless: true cannot be combined with lockOnly: true"),
            (exclusive, "clusterless: true requires exclusive: false"),
            (
                cluster,
                "clusterless: true cannot be combined with cluster specification",
            ),
        ]:
            if cond:
                set_terminal_phase(patch, Phase.FAILED, msg)
                return
        patch.status["phase"] = Phase.RESOLVING
        patch.status["message"] = "Resolving job requirements"
        return
    if exclusive and not cluster:
        set_terminal_phase(
            patch, Phase.FAILED, "exclusive: true requires 'cluster' to be set"
        )
        return

    if cluster:
        try:
            known_flavors = ctx.kueue.list_flavors()
        except k8s_client.exceptions.ApiException as exc:
            set_terminal_phase(
                patch, Phase.FAILED, f"Failed to list clusters: {exc.reason}"
            )
            logger.error("Job %s: list_flavors failed: %s", name, exc.reason)
            return
        if cluster not in known_flavors:
            set_terminal_phase(patch, Phase.FAILED, f"Cluster '{cluster}' not found")
            return

    if exclusive:
        patch.meta.setdefault("labels", {})[LABEL_EXCLUSIVE_CLUSTER] = cluster

    if lock_only:
        _create_lock_workload(spec, name, patch, body)
        return

    patch.status["phase"] = Phase.RESOLVING
    patch.status["message"] = "Resolving job requirements"
    logger.info("Job %s: phase=Resolving", name)


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------


def is_lock_only(spec) -> bool:
    """Return whether this spec describes a lockOnly job.

    If ``lockOnly`` is set explicitly (true or false), that value wins.
    If it's absent, it's inferred from the presence of ``lockUntil`` — a
    bare ``lockUntil`` is enough to imply a timed lock job, so callers
    don't have to write both fields together.
    """
    explicit = spec.get("lockOnly")
    if explicit is not None:
        return explicit
    return bool(spec.get("lockUntil"))


def parse_iso_timestamp(raw: str | None, field: str) -> datetime | None:
    """Parse *raw* as an ISO 8601 timestamp, defaulting to UTC if tz-naive.

    Returns None if *raw* is None. Raises ValueError, naming *field*, if
    *raw* is not a valid ISO 8601 timestamp.
    """
    if raw is None:
        return None
    try:
        ts = datetime.fromisoformat(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid {field}: {raw!r}") from exc
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts


def _parse_scheduled_time(spec) -> datetime | None:
    """Return the parsed scheduledStartTime, or None if absent. Raises ValueError if invalid."""
    return parse_iso_timestamp(spec.get("scheduledStartTime"), "scheduledStartTime")


# ---------------------------------------------------------------------------
# SCHEDULED — wait until scheduledStartTime is reached
# ---------------------------------------------------------------------------


def reconcile_scheduled(spec, name, namespace, status, patch, body):
    """Transition from Scheduled to the normal on_create flow once the time is reached."""
    try:
        scheduled_time = _parse_scheduled_time(spec)
    except ValueError as exc:
        set_terminal_phase(patch, Phase.FAILED, str(exc))
        return
    if scheduled_time is not None and datetime.now(UTC) < scheduled_time:
        return

    logger.info("Job %s: scheduled time reached, starting job", name)
    on_create(spec, name, namespace, {}, patch, body)


# ---------------------------------------------------------------------------
# RECURRING — create child FournosJobs on each cron tick
# ---------------------------------------------------------------------------


def reconcile_recurring(spec, name, namespace, status, patch, body):
    """Create a child FournosJob when the next cron tick is reached or trigger-now is set."""
    cron_expr = spec.get("schedule")
    if not cron_expr:
        return

    now = datetime.now(UTC)
    annotations = body.get("metadata", {}).get("annotations") or {}
    trigger_now = annotations.get(ANNOTATION_TRIGGER_NOW, "").lower() == "true"

    if not trigger_now:
        creation_time = datetime.fromisoformat(body["metadata"]["creationTimestamp"])

        last_raw = status.get("lastScheduledTime")
        if last_raw:
            try:
                base_time = datetime.fromisoformat(last_raw)
                if base_time.tzinfo is None:
                    base_time = base_time.replace(tzinfo=UTC)
            except (ValueError, TypeError):
                logger.warning(
                    "Job %s: corrupt lastScheduledTime %r, falling back to creationTimestamp",
                    name,
                    last_raw,
                )
                base_time = creation_time
        else:
            base_time = creation_time

        next_time = croniter(cron_expr, base_time).get_next(datetime)
        if next_time.tzinfo is None:
            next_time = next_time.replace(tzinfo=UTC)

        if now < next_time:
            return

    child_spec = copy.deepcopy(dict(spec))
    child_spec.pop("schedule", None)
    child_spec.pop("scheduledStartTime", None)

    safe_name = name[:58].rstrip("-")
    child_body = {
        "apiVersion": f"{CRD_GROUP}/{CRD_VERSION}",
        "kind": "FournosJob",
        "metadata": {
            "generateName": f"{safe_name}-",
            "namespace": namespace,
            "labels": {
                LABEL_RECURRING_PARENT: name,
            },
        },
        "spec": child_spec,
    }

    custom = k8s_client.CustomObjectsApi()
    try:
        created = custom.create_namespaced_custom_object(
            CRD_GROUP,
            CRD_VERSION,
            namespace,
            "fournosjobs",
            child_body,
        )
        child_name = created["metadata"]["name"]
        patch.status["lastScheduledTime"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        patch.status["message"] = (
            f"Recurring schedule: {cron_expr} (last child: {child_name})"
        )
        if trigger_now:
            patch.meta.setdefault("annotations", {})[ANNOTATION_TRIGGER_NOW] = "false"
            logger.info("Job %s: trigger-now created child %s", name, child_name)
        else:
            logger.info(
                "Job %s: created recurring child %s (next after %s)",
                name,
                child_name,
                next_time.isoformat(),
            )
    except k8s_client.exceptions.ApiException as exc:
        logger.error("Job %s: failed to create recurring child: %s", name, exc.reason)
        patch.status["message"] = f"Failed to create child job: {exc.reason}"


def _create_lock_workload(spec, name, patch, body):
    """Create a Kueue Workload that holds cluster-slot quota without running a pipeline."""
    conditions = []
    try:
        ctx.kueue.create_workload(
            name=name,
            gpu_type=None,
            gpu_count=0,
            cluster=spec["cluster"],
            exclusive=True,
            priority=spec.get("priority"),
            owner_ref=owner_ref(body),
        )
    except k8s_client.exceptions.ApiException as exc:
        if exc.status != 409:
            raise
        logger.debug("Job %s: lock Workload already exists (409)", name)

    patch.status["phase"] = Phase.PENDING
    patch.status["message"] = "Cluster lock pending admission"
    set_condition(
        patch,
        conditions,
        COND_WORKLOAD_ADMITTED,
        "False",
        "Pending",
        "Lock workload created, waiting for Kueue admission",
    )
    logger.info("Job %s: lockOnly=true, created lock Workload, phase=Pending", name)


# ---------------------------------------------------------------------------
# PENDING — wait for Kueue admission
# ---------------------------------------------------------------------------


def _find_exclusive_locker(cluster: str, exclude_job: str) -> str | None:
    """Return the name of the exclusive job actively holding *cluster*, if any.

    Only jobs in Admitted or Running phase actually hold cluster-slot quota.
    Returns None on API errors so reconciliation is not interrupted.
    """
    try:
        custom = k8s_client.CustomObjectsApi()
        jobs = custom.list_namespaced_custom_object(
            CRD_GROUP,
            CRD_VERSION,
            settings.workload_namespace,
            "fournosjobs",
            label_selector=f"{LABEL_EXCLUSIVE_CLUSTER}={cluster}",
        )
    except k8s_client.exceptions.ApiException:
        logger.warning("Failed to query exclusive locker for cluster %s", cluster)
        return None
    for job in jobs.get("items", []):
        job_name = job["metadata"]["name"]
        if job_name == exclude_job:
            continue
        phase = job.get("status", {}).get("phase", "")
        if phase in LOCK_HOLDING_PHASES:
            return job_name
    return None


def _pending_status(
    wl_message: str,
    cluster: str | None,
    exclusive: bool,
    locker: str | None = None,
) -> tuple[str, str]:
    """Return (user_message, log_message) with cluster-slot context when applicable."""
    if not wl_message:
        return "Waiting for Kueue admission", "Workload pending admission"

    is_slot_issue = CLUSTER_SLOT_RESOURCE in wl_message

    if is_slot_issue and exclusive and cluster:
        user_msg = (
            f"Waiting for exclusive access to cluster {cluster} "
            f"(other jobs are still running)"
        )
        log_msg = (
            f"exclusive job waiting for cluster {cluster} to clear (slot contention)"
        )
    elif is_slot_issue and cluster:
        locker_label = f"job {locker}" if locker else "another job"
        user_msg = (
            f"Cluster {cluster} is exclusively locked by {locker_label}, "
            f"waiting for it to finish"
        )
        if locker:
            log_msg = (
                f"cluster {cluster} exclusively locked by {locker} (slot contention)"
            )
        else:
            log_msg = (
                f"cluster {cluster} exclusively locked (slot contention, "
                f"locker not found — may have just finished)"
            )
    elif is_slot_issue:
        user_msg = (
            "All eligible clusters are exclusively locked, waiting for availability"
        )
        log_msg = "hardware-only job blocked by exclusive locks (slot contention)"
    else:
        user_msg = f"Waiting for admission: {wl_message}"
        log_msg = "Workload pending admission"

    return user_msg, log_msg


def _redirect_to_equivalent_cluster(spec, name, status, patch, body) -> bool:
    """If pinned to a cluster Hearth has marked unreachable, drop the pin and
    let Kueue pick any flavor offering the same GPU type. Returns True if a
    redirect was performed this reconcile.

    Only applies to jobs that specify both `cluster` and `hardware` — a
    cluster-lock-only job (no GPU type) has no "equivalent" to redirect to,
    since the user explicitly wanted that one cluster.
    """
    cluster = spec.get("cluster")
    hardware = spec.get("hardware") or {}
    gpu_type = hardware.get("gpuType")

    if not cluster or not gpu_type:
        return False
    if status.get("redirectedFrom") == cluster:
        return False
    if ctx.kueue.is_flavor_healthy(cluster):
        return False

    logger.warning(
        "Job %s: pinned cluster %s is unreachable, redirecting to an equivalent cluster",
        name,
        cluster,
    )
    ctx.kueue.delete_workload(name)
    ctx.kueue.create_workload(
        name=name,
        gpu_type=gpu_type,
        gpu_count=hardware.get("gpuCount", 0),
        cluster=None,
        exclusive=spec["exclusive"],
        priority=spec.get("priority"),
        owner_ref=owner_ref(body),
    )

    patch.status["redirectedFrom"] = cluster
    patch.status["redirectCount"] = status.get("redirectCount", 0) + 1
    patch.status["message"] = (
        f"Cluster {cluster} is unreachable; redirected to an equivalent cluster"
    )
    set_condition(
        patch,
        list(status.get("conditions") or []),
        COND_WORKLOAD_ADMITTED,
        "False",
        "Redirected",
        f"Original cluster {cluster} unreachable; retrying on an equivalent cluster",
    )
    logger.info("Job %s: redirected away from unreachable cluster %s", name, cluster)
    return True


def reconcile_pending(spec, name, status, patch, body):
    wl = ctx.kueue.get_workload_or_none(name)
    if wl is None:
        logger.info("Job %s: Workload not yet visible", name)
        return

    conditions = list(status.get("conditions") or [])

    if not KueueClient.is_admitted(wl):
        if _redirect_to_equivalent_cluster(spec, name, status, patch, body):
            return

        wl_reason, wl_message = KueueClient.get_pending_message(wl)
        cluster = spec.get("cluster")
        already_redirected = status.get("redirectedFrom") == cluster
        locker = None
        if cluster and not already_redirected and CLUSTER_SLOT_RESOURCE in wl_message:
            locker = _find_exclusive_locker(cluster, name)
        new_msg, log_msg = _pending_status(
            wl_message,
            None if already_redirected else cluster,
            spec["exclusive"],
            locker,
        )
        if status.get("message") != new_msg:
            patch.status["message"] = new_msg
            set_condition(
                patch,
                conditions,
                COND_WORKLOAD_ADMITTED,
                "False",
                wl_reason or "Pending",
                new_msg,
            )
        logger.info("Job %s: %s", name, log_msg)
        return

    # --- Workload admitted ---
    assigned_cluster = KueueClient.get_assigned_flavor(wl)
    if not assigned_cluster:
        set_terminal_phase(
            patch, Phase.FAILED, "Workload admitted but no flavor assigned"
        )
        set_condition(
            patch,
            conditions,
            COND_WORKLOAD_ADMITTED,
            "False",
            "NoFlavorAssigned",
            "Workload was admitted but no ResourceFlavor was assigned",
        )
        ctx.kueue.delete_workload(name)
        logger.error("Job %s: admitted without assigned flavor", name)
        return

    patch.status["phase"] = Phase.ADMITTED
    patch.status["cluster"] = assigned_cluster
    patch.status["message"] = (
        f"Workload admitted, assigned to cluster {assigned_cluster}"
    )
    set_condition(
        patch,
        conditions,
        COND_WORKLOAD_ADMITTED,
        "True",
        "Admitted",
        f"Assigned to cluster {assigned_cluster}",
    )
    logger.info("Job %s: Workload admitted, cluster=%s", name, assigned_cluster)
