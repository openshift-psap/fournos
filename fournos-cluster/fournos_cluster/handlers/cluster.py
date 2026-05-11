from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from kubernetes import client

from fournos_cluster.constants import (
    COND_GPU_DISCOVERED,
    COND_KUBECONFIG_VALID,
    CRD_GROUP,
    CRD_VERSION,
    FOURNOSJOB_PLURAL,
    LABEL_CLUSTER_LOCK,
)
from fournos_cluster.core.gpu_discovery import GPUDiscoveryError
from fournos_cluster.settings import settings
from fournos_cluster.state import ctx

logger = logging.getLogger(__name__)

_DURATION_RE = re.compile(r"^(\d+)(m|h|d)$")
_DURATION_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def parse_duration(s: str) -> timedelta:
    match = _DURATION_RE.match(s)
    if not match:
        raise ValueError(f"Invalid duration: {s!r}")
    value, unit = int(match.group(1)), match.group(2)
    return timedelta(**{_DURATION_UNITS[unit]: value})


def _make_condition(
    cond_type: str,
    status: str,
    reason: str,
    message: str = "",
) -> dict:
    return {
        "type": cond_type,
        "status": status,
        "reason": reason,
        "message": message,
        "lastTransitionTime": datetime.now(timezone.utc).isoformat(),
    }


def _build_gpu_summary(gpus: list[dict]) -> str:
    if not gpus:
        return ""
    parts = [f"{g['count']}x {g['shortName'].upper()}" for g in gpus]
    return ", ".join(parts)


def _check_kubeconfig(spec: dict) -> str:
    secret_name = spec["kubeconfigSecret"]
    core = client.CoreV1Api()
    try:
        core.read_namespaced_secret(secret_name, settings.secrets_namespace)
        return "Valid"
    except Exception as exc:
        if hasattr(exc, "status") and exc.status == 404:
            return "Missing"
        logger.warning("Error checking kubeconfig secret %s: %s", secret_name, exc)
        return "Invalid"


def _lock_job_name(cluster_name: str) -> str:
    return f"cluster-lock-{cluster_name}"


# ---------------------------------------------------------------------------
# CREATE / RESUME
# ---------------------------------------------------------------------------


def on_cluster_create(spec, name, namespace, status, patch, body, **kwargs):
    logger.info("FournosCluster %s: initializing", name)

    kubeconfig_status = _check_kubeconfig(spec)

    patch.status["kubeconfigStatus"] = kubeconfig_status
    patch.status["locked"] = False
    patch.status["conditions"] = [
        _make_condition(
            COND_KUBECONFIG_VALID,
            "True" if kubeconfig_status == "Valid" else "False",
            kubeconfig_status,
        ),
        _make_condition(COND_GPU_DISCOVERED, "False", "Pending"),
    ]

    ctx.kueue.create_flavor(name)
    ctx.kueue.add_flavor_to_cluster_queue(name)

    owner = spec.get("owner")
    if owner:
        _apply_lock(spec, name, patch, owner)

    logger.info("FournosCluster %s: initialized (kubeconfig=%s)", name, kubeconfig_status)


# ---------------------------------------------------------------------------
# FIELD WATCHES
# ---------------------------------------------------------------------------


def on_owner_change(spec, name, namespace, status, patch, body, old, new, **kwargs):
    if new:
        _apply_lock(spec, name, patch, new)
    else:
        _release_lock(name, patch)


def on_hardware_change(spec, name, old, new, patch, **kwargs):
    if not new:
        return

    gpus = new.get("gpus", [])
    gpu_resources = [(g["shortName"], g["count"]) for g in gpus]

    if gpu_resources:
        try:
            ctx.kueue.update_flavor_quotas(name, gpu_resources)
            logger.info("FournosCluster %s: updated CQ quotas from spec.hardware", name)
        except Exception as exc:
            logger.warning("FournosCluster %s: failed to update flavor quotas: %s", name, exc)

    patch.status["gpuSummary"] = _build_gpu_summary(gpus)


# ---------------------------------------------------------------------------
# LOCKING
# ---------------------------------------------------------------------------


def _apply_lock(spec: dict, name: str, patch, owner: str) -> None:
    lock_job = _lock_job_name(name)
    _create_sentinel_job(name, lock_job, owner)

    now = datetime.now(timezone.utc)
    patch.status["locked"] = True
    patch.status["ownerSetAt"] = now.isoformat()
    patch.status["lockJobName"] = lock_job

    ttl_str = spec.get("ttl")
    if ttl_str:
        try:
            ttl = parse_duration(ttl_str)
            patch.status["lockExpiresAt"] = (now + ttl).isoformat()
        except ValueError:
            logger.warning("FournosCluster %s: invalid TTL %r, no expiry set", name, ttl_str)
            patch.status["lockExpiresAt"] = None
    else:
        patch.status["lockExpiresAt"] = None

    logger.info(
        "FournosCluster %s: locked by %s (sentinel=%s, ttl=%s)",
        name,
        owner,
        lock_job,
        ttl_str or "indefinite",
    )


def _release_lock(name: str, patch) -> None:
    lock_job = _lock_job_name(name)
    _delete_sentinel_job(lock_job)

    patch.status["locked"] = False
    patch.status["lockExpiresAt"] = None
    patch.status["ownerSetAt"] = None
    patch.status["lockJobName"] = None

    logger.info("FournosCluster %s: unlocked (deleted sentinel %s)", name, lock_job)


def _create_sentinel_job(cluster_name: str, job_name: str, owner: str) -> None:
    body = {
        "apiVersion": f"{CRD_GROUP}/{CRD_VERSION}",
        "kind": "FournosJob",
        "metadata": {
            "name": job_name,
            "namespace": settings.namespace,
            "labels": {
                LABEL_CLUSTER_LOCK: cluster_name,
            },
        },
        "spec": {
            "cluster": cluster_name,
            "exclusive": True,
            "lockOnly": True,
            "owner": owner,
            "displayName": f"Cluster lock: {cluster_name} (owner: {owner})",
            "executionEngine": {"none": {}},
        },
    }
    custom = client.CustomObjectsApi()
    try:
        custom.create_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=settings.namespace,
            plural=FOURNOSJOB_PLURAL,
            body=body,
        )
        logger.info("Created sentinel FournosJob %s for cluster %s", job_name, cluster_name)
    except client.exceptions.ApiException as exc:
        if exc.status == 409:
            logger.debug("Sentinel FournosJob %s already exists", job_name)
        else:
            raise


def _delete_sentinel_job(job_name: str) -> None:
    custom = client.CustomObjectsApi()
    try:
        custom.delete_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=settings.namespace,
            plural=FOURNOSJOB_PLURAL,
            name=job_name,
        )
        logger.info("Deleted sentinel FournosJob %s", job_name)
    except client.exceptions.ApiException as exc:
        if exc.status == 404:
            logger.debug("Sentinel FournosJob %s already deleted", job_name)
        else:
            raise


# ---------------------------------------------------------------------------
# TIMER — GPU discovery, TTL expiry, self-healing
# ---------------------------------------------------------------------------


def reconcile(spec, name, namespace, status, patch, body, **kwargs):
    _reconcile_kubeconfig(spec, name, status, patch)
    _reconcile_ttl_expiry(spec, name, status, patch)
    _reconcile_gpu_discovery(spec, name, status, patch)
    _reconcile_lock_job(spec, name, status, patch)


def _reconcile_kubeconfig(spec, name, status, patch):
    current = _check_kubeconfig(spec)
    prev = status.get("kubeconfigStatus")
    if current != prev:
        patch.status["kubeconfigStatus"] = current
        patch.status.setdefault("conditions", []).append(
            _make_condition(
                COND_KUBECONFIG_VALID,
                "True" if current == "Valid" else "False",
                current,
            )
        )
        logger.info("FournosCluster %s: kubeconfigStatus changed %s -> %s", name, prev, current)


def _reconcile_ttl_expiry(spec, name, status, patch):
    if not status.get("locked"):
        return

    expires_at_str = status.get("lockExpiresAt")
    if not expires_at_str:
        return

    try:
        expires_at = datetime.fromisoformat(expires_at_str)
    except (ValueError, TypeError):
        return

    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if datetime.now(timezone.utc) >= expires_at:
        prev_owner = spec.get("owner", "unknown")
        logger.info(
            "FournosCluster %s: ownership expired (was owned by %s)", name, prev_owner
        )
        patch.spec["owner"] = ""
        _release_lock(name, patch)


def _reconcile_gpu_discovery(spec, name, status, patch):
    if status.get("kubeconfigStatus") != "Valid":
        return

    hardware = spec.get("hardware") or {}
    last_discovery_str = hardware.get("lastDiscovery")

    interval_str = spec.get("gpuDiscoveryInterval", "5m")
    try:
        interval = parse_duration(interval_str)
    except ValueError:
        interval = timedelta(seconds=settings.gpu_discovery_default_interval_sec)

    consecutive_failures = hardware.get("consecutiveFailures", 0)
    if consecutive_failures >= 3:
        backoff_multiplier = min(2 ** (consecutive_failures - 2), 6)
        interval = interval * backoff_multiplier

    if last_discovery_str:
        try:
            last_discovery = datetime.fromisoformat(last_discovery_str)
            if last_discovery.tzinfo is None:
                last_discovery = last_discovery.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - last_discovery < interval:
                return
        except (ValueError, TypeError):
            pass

    try:
        result = ctx.gpu_discovery.discover_gpus(
            name, spec["kubeconfigSecret"], settings.secrets_namespace
        )
    except GPUDiscoveryError as exc:
        failures = consecutive_failures + 1
        patch.spec.setdefault("hardware", {})["consecutiveFailures"] = failures
        patch.spec["hardware"]["lastError"] = str(exc)

        if failures >= 5:
            patch.status["kubeconfigStatus"] = "Unreachable"

        patch.status.setdefault("conditions", []).append(
            _make_condition(
                COND_GPU_DISCOVERED,
                "False",
                "DiscoveryFailed",
                str(exc),
            )
        )
        logger.warning(
            "FournosCluster %s: GPU discovery failed (%d consecutive): %s",
            name,
            failures,
            exc,
        )
        return

    gpu_dicts = [
        {
            "vendor": g.vendor,
            "model": g.model,
            "shortName": g.short_name,
            "count": g.count,
            "nodeCount": g.node_count,
        }
        for g in result.gpus
    ]

    patch.spec["hardware"] = {
        "gpus": gpu_dicts,
        "totalGPUs": result.total_gpus,
        "lastDiscovery": result.timestamp,
        "consecutiveFailures": 0,
        "lastError": None,
    }
    patch.status.setdefault("conditions", []).append(
        _make_condition(COND_GPU_DISCOVERED, "True", "Discovered")
    )


def _reconcile_lock_job(spec, name, status, patch):
    if not status.get("locked"):
        return

    lock_job = _lock_job_name(name)
    custom = client.CustomObjectsApi()
    try:
        custom.get_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=settings.namespace,
            plural=FOURNOSJOB_PLURAL,
            name=lock_job,
        )
    except client.exceptions.ApiException as exc:
        if exc.status == 404:
            owner = spec.get("owner")
            if owner:
                logger.warning(
                    "FournosCluster %s: sentinel job %s missing, recreating", name, lock_job
                )
                _create_sentinel_job(name, lock_job, owner)
            else:
                logger.warning(
                    "FournosCluster %s: sentinel job %s missing and no owner, clearing lock",
                    name,
                    lock_job,
                )
                _release_lock(name, patch)
        else:
            logger.warning(
                "FournosCluster %s: failed to check sentinel job: %s", name, exc
            )
