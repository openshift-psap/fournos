"""Lifecycle handlers — on_create and reconcile_pending.

Covers the early phases of a FournosJob: creation and pending admission
through Kueue.  Exclusive locking is handled entirely by Kueue via
cluster-slot resources.
"""

from __future__ import annotations

import logging

from kubernetes import client

from fournos.core.constants import CLUSTER_SLOT_RESOURCE, Phase
from fournos.core.kueue import KueueClient
from fournos.state import ctx

from .status import (
    COND_WORKLOAD_ADMITTED,
    create_workload_for_job,
    set_condition,
    utcnow,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CREATE / RESUME handler
# ---------------------------------------------------------------------------


def on_create(spec, name, namespace, status, patch, body):
    if status.get("phase"):
        return

    cluster = spec.get("cluster")
    hardware = spec.get("hardware")
    exclusive = spec.get("exclusive", False)

    if not cluster and not hardware:
        patch.status["phase"] = Phase.FAILED
        patch.status["message"] = "Must specify 'cluster', 'hardware', or both"
        return

    if exclusive and not cluster:
        patch.status["phase"] = Phase.FAILED
        patch.status["message"] = "exclusive: true requires 'cluster' to be set"
        return

    if cluster:
        try:
            known_flavors = ctx.kueue.list_flavors()
        except client.exceptions.ApiException as exc:
            patch.status["phase"] = Phase.FAILED
            patch.status["message"] = f"Failed to list clusters: {exc.reason}"
            logger.error("Job %s: list_flavors failed: %s", name, exc.reason)
            return
        if cluster not in known_flavors:
            patch.status["phase"] = Phase.FAILED
            patch.status["message"] = f"Cluster '{cluster}' not found"
            return

    if hardware and hardware.get("gpuType"):
        gpu_type = hardware["gpuType"]
        try:
            known_gpu_types = ctx.kueue.list_gpu_types()
        except client.exceptions.ApiException as exc:
            patch.status["phase"] = Phase.FAILED
            patch.status["message"] = f"Failed to list GPU types: {exc.reason}"
            logger.error("Job %s: list_gpu_types failed: %s", name, exc.reason)
            return
        if not known_gpu_types:
            patch.status["phase"] = Phase.FAILED
            patch.status["message"] = "No GPU types configured"
            logger.error("Job %s: no GPU types found in any ClusterQueue", name)
            return
        if gpu_type not in known_gpu_types:
            patch.status["phase"] = Phase.FAILED
            patch.status["message"] = (
                f"GPU type '{gpu_type}' not available. "
                f"Valid types: {', '.join(sorted(known_gpu_types))}"
            )
            return

    try:
        create_workload_for_job(spec, name, body)
    except client.exceptions.ApiException as exc:
        if exc.status == 409:
            pass
        else:
            patch.status["phase"] = Phase.FAILED
            patch.status["message"] = f"Failed to create Workload: {exc.reason}"
            logger.error(
                "Job %s: Workload creation failed: %s\n%s", name, exc.reason, exc.body
            )
            return

    patch.status["phase"] = Phase.PENDING
    patch.status["message"] = "Workload created, waiting for Kueue admission"
    patch.status["conditions"] = [
        {
            "type": COND_WORKLOAD_ADMITTED,
            "status": "False",
            "reason": "Pending",
            "message": "Workload created, waiting for Kueue admission",
            "lastTransitionTime": utcnow(),
        }
    ]
    logger.info("Job %s: created Workload, phase=Pending", name)


# ---------------------------------------------------------------------------
# PENDING — wait for Kueue admission
# ---------------------------------------------------------------------------


def _pending_status(
    wl_message: str,
    cluster: str | None,
    exclusive: bool,
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
        user_msg = (
            f"Cluster {cluster} is exclusively locked by another job, "
            f"waiting for it to finish"
        )
        log_msg = f"cluster {cluster} exclusively locked (slot contention)"
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
        new_msg, log_msg = _pending_status(
            wl_message,
            spec.get("cluster"),
            spec.get("exclusive", False),
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
