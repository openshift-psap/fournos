"""Lifecycle handlers — on_create and reconcile_blocked / reconcile_pending.

Covers the early phases of a FournosJob: creation, blocking, and pending
admission through Kueue.
"""

from __future__ import annotations

import logging

from kubernetes import client

from fournos.core.constants import LABEL_EXCLUSIVE_CLUSTER, Phase
from fournos.core.kueue import KueueClient
from fournos.core.locking import get_locked_clusters, is_cluster_occupied
from fournos.state import ctx

from .status import (
    COND_CLUSTER_LOCKED,
    COND_WORKLOAD_ADMITTED,
    create_workload_for_job,
    set_blocked,
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
        if known_gpu_types and gpu_type not in known_gpu_types:
            patch.status["phase"] = Phase.FAILED
            patch.status["message"] = (
                f"GPU type '{gpu_type}' not available. "
                f"Valid types: {', '.join(sorted(known_gpu_types))}"
            )
            return

    # --- Exclusive-lock label ---
    if exclusive:
        patch.meta.setdefault("labels", {})[LABEL_EXCLUSIVE_CLUSTER] = cluster

    # --- Lock / occupancy gate ---
    conditions: list[dict] = []

    if exclusive:
        active_jobs = is_cluster_occupied(cluster, name)
        if active_jobs:
            set_blocked(
                patch,
                conditions,
                "ClusterOccupied",
                f"Waiting for cluster {cluster} to clear "
                f"(occupied by {', '.join(sorted(active_jobs))})",
            )
            logger.info("Job %s: cluster %s occupied, phase=Blocked", name, cluster)
            return

    elif cluster:
        locks = get_locked_clusters()
        if cluster in locks:
            set_blocked(
                patch,
                conditions,
                "ClusterLocked",
                f"Cluster {cluster} is locked by exclusive job {locks[cluster]}",
            )
            logger.info(
                "Job %s: cluster %s locked by %s, phase=Blocked",
                name,
                cluster,
                locks[cluster],
            )
            return

    # Hardware-only jobs: exclude locked clusters via nodeAffinity
    exclude = None
    if not cluster:
        locks = get_locked_clusters()
        if locks:
            exclude = list(locks.keys())

    try:
        create_workload_for_job(spec, name, body, exclude_clusters=exclude)
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
# BLOCKED — re-check whether the blocking condition has cleared
# ---------------------------------------------------------------------------


def reconcile_blocked(spec, name, status, patch, body):
    """Re-check whether the blocking condition has cleared."""
    cluster = spec.get("cluster")
    exclusive = spec.get("exclusive", False)
    conditions = list(status.get("conditions") or [])

    if exclusive:
        active_jobs = is_cluster_occupied(cluster, name)
        if active_jobs:
            new_msg = (
                f"Waiting for cluster {cluster} to clear "
                f"(occupied by {', '.join(sorted(active_jobs))})"
            )
            if status.get("message") != new_msg:
                set_blocked(patch, conditions, "ClusterOccupied", new_msg)
            logger.info("Job %s: cluster %s still occupied", name, cluster)
            return
    elif cluster:
        locks = get_locked_clusters()
        if cluster in locks:
            new_msg = f"Cluster {cluster} is locked by exclusive job {locks[cluster]}"
            if status.get("message") != new_msg:
                set_blocked(patch, conditions, "ClusterLocked", new_msg)
            logger.info("Job %s: cluster %s still locked", name, cluster)
            return
    else:
        # Hardware-only job bounced back from Pending (post-admission race).
        pass

    # Block condition cleared — create Workload and transition to Pending.
    exclude = None
    if not cluster:
        locks = get_locked_clusters()
        if locks:
            exclude = list(locks.keys())

    try:
        create_workload_for_job(spec, name, body, exclude_clusters=exclude)
    except client.exceptions.ApiException as exc:
        if exc.status == 409:
            pass
        else:
            patch.status["phase"] = Phase.FAILED
            patch.status["message"] = f"Failed to create Workload: {exc.reason}"
            logger.error("Job %s: Workload creation failed: %s", name, exc.reason)
            return

    patch.status["phase"] = Phase.PENDING
    patch.status["message"] = "Workload created, waiting for Kueue admission"
    set_condition(
        patch,
        conditions,
        COND_CLUSTER_LOCKED,
        "False",
        "Cleared",
        "Cluster is available",
    )
    set_condition(
        patch,
        conditions,
        COND_WORKLOAD_ADMITTED,
        "False",
        "Pending",
        "Workload created, waiting for Kueue admission",
    )
    logger.info("Job %s: block cleared, created Workload, phase=Pending", name)


# ---------------------------------------------------------------------------
# PENDING — wait for Kueue admission, reconcile anti-affinity
# ---------------------------------------------------------------------------


def reconcile_pending(spec, name, status, patch, body):
    cluster = spec.get("cluster")
    exclusive = spec.get("exclusive", False)

    wl = ctx.kueue.get_workload_or_none(name)
    if wl is None:
        logger.info("Job %s: Workload not yet visible", name)
        return

    conditions = list(status.get("conditions") or [])

    if not KueueClient.is_admitted(wl):
        # --- Stale anti-affinity reconciliation for hardware-only jobs ---
        if not cluster:
            current_excluded = KueueClient.get_excluded_clusters(wl)
            locks = get_locked_clusters()
            desired_excluded = sorted(locks.keys()) if locks else []
            if current_excluded != desired_excluded:
                ctx.kueue.delete_workload(name)
                try:
                    create_workload_for_job(
                        spec,
                        name,
                        body,
                        exclude_clusters=desired_excluded or None,
                    )
                except client.exceptions.ApiException as exc:
                    if exc.status != 409:
                        raise
                logger.info(
                    "Job %s: recreated Workload with updated exclusion list %s",
                    name,
                    desired_excluded,
                )
                return

        wl_reason, wl_message = KueueClient.get_pending_message(wl)
        new_msg = (
            f"Waiting for admission: {wl_message}"
            if wl_message
            else "Waiting for Kueue admission"
        )
        if status.get("message") != new_msg:
            patch.status["message"] = new_msg
            set_condition(
                patch,
                conditions,
                COND_WORKLOAD_ADMITTED,
                "False",
                wl_reason or "Pending",
                wl_message or "Workload is queued for Kueue admission",
            )
        logger.info("Job %s: Workload pending admission", name)
        return

    # --- Workload admitted — post-admission safety checks ---
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

    if exclusive:
        active_jobs = is_cluster_occupied(assigned_cluster, name)
        if active_jobs:
            logger.info(
                "Job %s: exclusive but cluster %s still occupied by %s, staying Pending",
                name,
                assigned_cluster,
                active_jobs,
            )
            new_msg = (
                f"Admitted to {assigned_cluster} but waiting for cluster to clear "
                f"(occupied by {', '.join(sorted(active_jobs))})"
            )
            if status.get("message") != new_msg:
                patch.status["message"] = new_msg
            return

    if not exclusive:
        locks = get_locked_clusters()
        if assigned_cluster in locks:
            ctx.kueue.delete_workload(name)
            set_blocked(
                patch,
                conditions,
                "ClusterLocked",
                f"Cluster {assigned_cluster} is locked by exclusive job "
                f"{locks[assigned_cluster]}",
            )
            logger.info(
                "Job %s: admitted to locked cluster %s, moving to Blocked",
                name,
                assigned_cluster,
            )
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
