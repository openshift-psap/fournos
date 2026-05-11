"""Lifecycle handlers — on_create and reconcile_pending.

Covers the early phases of a FournosJob: creation and pending admission
through Kueue.  Exclusive locking is handled entirely by Kueue via
cluster-slot resources.
"""

from __future__ import annotations

import logging

from kubernetes import client as k8s_client

from fournos.core.constants import (
    CLUSTER_SLOT_RESOURCE,
    LABEL_EXCLUSIVE_CLUSTER,
    LOCK_HOLDING_PHASES,
    Phase,
)
from fournos.core.kueue import KueueClient
from fournos.settings import settings
from fournos.state import ctx

from .status import (
    CRD_GROUP,
    CRD_VERSION,
    COND_WORKLOAD_ADMITTED,
    owner_ref,
    set_condition,
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
        patch.status["phase"] = Phase.STOPPED
        patch.status["message"] = "Job stopped by user"
        logger.info("Job %s: created with shutdown=%s, skipping", name, shutdown)
        return

    cluster = spec.get("cluster")
    exclusive = spec["exclusive"]
    lock_only = spec.get("lockOnly", False)

    if lock_only and not cluster:
        patch.status["phase"] = Phase.FAILED
        patch.status["message"] = "lockOnly: true requires 'cluster' to be set"
        return

    engine = spec.get("executionEngine") or {}
    if not lock_only and not engine:
        patch.status["phase"] = Phase.FAILED
        patch.status["message"] = "spec.executionEngine is required for non-lockOnly jobs"
        return

    if exclusive and not cluster:
        patch.status["phase"] = Phase.FAILED
        patch.status["message"] = "exclusive: true requires 'cluster' to be set"
        return

    if cluster:
        try:
            known_flavors = ctx.kueue.list_flavors()
        except k8s_client.exceptions.ApiException as exc:
            patch.status["phase"] = Phase.FAILED
            patch.status["message"] = f"Failed to list clusters: {exc.reason}"
            logger.error("Job %s: list_flavors failed: %s", name, exc.reason)
            return
        if cluster not in known_flavors:
            patch.status["phase"] = Phase.FAILED
            patch.status["message"] = f"Cluster '{cluster}' not found"
            return

    if exclusive:
        patch.meta.setdefault("labels", {})[LABEL_EXCLUSIVE_CLUSTER] = cluster

    if lock_only:
        _create_lock_workload(spec, name, patch, body)
        return

    patch.status["phase"] = Phase.RESOLVING
    patch.status["message"] = "Resolving job requirements"
    logger.info("Job %s: phase=Resolving", name)


def _create_lock_workload(spec, name, patch, body):
    """Create a Kueue Workload for a lockOnly sentinel job and go straight to Pending."""
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
            patch.status["phase"] = Phase.FAILED
            patch.status["message"] = f"Failed to create Workload: {exc.reason}"
            logger.error("Job %s: Workload creation failed: %s", name, exc.reason)
            return

    patch.status["phase"] = Phase.PENDING
    patch.status["message"] = "Cluster lock pending admission"
    set_condition(
        patch,
        [],
        COND_WORKLOAD_ADMITTED,
        "False",
        "Pending",
        "Lock workload created, waiting for Kueue admission",
    )
    logger.info("Job %s: lockOnly sentinel, phase=Pending", name)


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


def reconcile_pending(spec, name, status, patch, body):
    wl = ctx.kueue.get_workload_or_none(name)
    if wl is None:
        logger.info("Job %s: Workload not yet visible", name)
        return

    conditions = list(status.get("conditions") or [])

    if not KueueClient.is_admitted(wl):
        wl_reason, wl_message = KueueClient.get_pending_message(wl)
        cluster = spec.get("cluster")
        locker = None
        if cluster and CLUSTER_SLOT_RESOURCE in wl_message:
            locker = _find_exclusive_locker(cluster, name)
        new_msg, log_msg = _pending_status(
            wl_message,
            cluster,
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
        patch.status["phase"] = Phase.FAILED
        patch.status["message"] = "Workload admitted but no flavor assigned"
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
