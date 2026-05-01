from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from kubernetes import client

from fournos.core.constants import (
    COND_GPU_DISCOVERED,
    COND_KUBECONFIG_VALID,
    LABEL_PSAPCLUSTER_LOCK,
)
from fournos.core.gpu_discovery import GPUDiscoveryError
from fournos.settings import settings
from fournos.state import ctx

logger = logging.getLogger(__name__)

_DURATION_RE = re.compile(r"^(\d+)(m|h|d)$")
_DURATION_UNITS = {"m": "minutes", "h": "hours", "d": "days"}

CRD_GROUP = "fournos.dev"
CRD_VERSION = "v1"
FOURNOSJOB_PLURAL = "fournosjobs"


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
    try:
        ctx.registry._k8s.read_namespaced_secret(
            secret_name, settings.secrets_namespace
        )
        return "Valid"
    except Exception as exc:
        if hasattr(exc, "status") and exc.status == 404:
            return "Missing"
        logger.warning("Error checking kubeconfig secret %s: %s", secret_name, exc)
        return "Invalid"


def _lock_job_name(cluster_name: str) -> str:
    return f"psapcluster-lock-{cluster_name}"


# ---------------------------------------------------------------------------
# CREATE / RESUME
# ---------------------------------------------------------------------------


def on_psapcluster_create(spec, name, namespace, status, patch, body):
    logger.info("PSAPCluster %s: initializing", name)

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

    owner = spec.get("owner")
    if owner:
        _apply_lock(spec, name, patch, owner)

    logger.info("PSAPCluster %s: initialized (kubeconfig=%s)", name, kubeconfig_status)


# ---------------------------------------------------------------------------
# OWNER FIELD CHANGE
# ---------------------------------------------------------------------------


def on_psapcluster_owner_change(spec, name, namespace, status, patch, body, old, new):
    if new:
        _apply_lock(spec, name, patch, new)
    else:
        _release_lock(name, patch)


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
            logger.warning("PSAPCluster %s: invalid TTL %r, no expiry set", name, ttl_str)
            patch.status["lockExpiresAt"] = None
    else:
        patch.status["lockExpiresAt"] = None

    logger.info(
        "PSAPCluster %s: locked by %s (sentinel=%s, ttl=%s)",
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

    logger.info("PSAPCluster %s: unlocked (deleted sentinel %s)", name, lock_job)


def _create_sentinel_job(cluster_name: str, job_name: str, owner: str) -> None:
    body = {
        "apiVersion": f"{CRD_GROUP}/{CRD_VERSION}",
        "kind": "FournosJob",
        "metadata": {
            "name": job_name,
            "namespace": settings.namespace,
            "labels": {
                LABEL_PSAPCLUSTER_LOCK: cluster_name,
            },
        },
        "spec": {
            "cluster": cluster_name,
            "exclusive": True,
            "lockOnly": True,
            "owner": owner,
            "displayName": f"Cluster lock: {cluster_name} (owner: {owner})",
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


def reconcile_psapcluster(spec, name, namespace, status, patch, body):
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
        logger.info("PSAPCluster %s: kubeconfigStatus changed %s -> %s", name, prev, current)


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
            "PSAPCluster %s: ownership expired (was owned by %s)", name, prev_owner
        )
        patch.spec["owner"] = ""
        _release_lock(name, patch)


def _reconcile_gpu_discovery(spec, name, status, patch):
    if status.get("kubeconfigStatus") != "Valid":
        return

    hardware = status.get("hardware") or {}
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
        patch.status.setdefault("hardware", {})["consecutiveFailures"] = failures
        patch.status["hardware"]["lastError"] = str(exc)

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
            "PSAPCluster %s: GPU discovery failed (%d consecutive): %s",
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

    patch.status["hardware"] = {
        "gpus": gpu_dicts,
        "totalGPUs": result.total_gpus,
        "lastDiscovery": result.timestamp,
        "consecutiveFailures": 0,
        "lastError": None,
    }
    patch.status["gpuSummary"] = _build_gpu_summary(gpu_dicts)
    patch.status.setdefault("conditions", []).append(
        _make_condition(COND_GPU_DISCOVERED, "True", "Discovered")
    )

    prev_gpus = hardware.get("gpus", [])
    new_resources = [(g.short_name, g.count) for g in result.gpus]
    prev_resources = [(g.get("shortName"), g.get("count")) for g in prev_gpus]
    if new_resources != prev_resources and new_resources:
        try:
            ctx.kueue.update_flavor_quotas(name, new_resources)
        except Exception as exc:
            logger.warning(
                "PSAPCluster %s: failed to update flavor quotas: %s", name, exc
            )


def _reconcile_lock_job(spec, name, status, patch):
    """Self-healing: if locked but sentinel job is missing, recreate or clear."""
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
                    "PSAPCluster %s: sentinel job %s missing, recreating", name, lock_job
                )
                _create_sentinel_job(name, lock_job, owner)
            else:
                logger.warning(
                    "PSAPCluster %s: sentinel job %s missing and no owner, clearing lock",
                    name,
                    lock_job,
                )
                _release_lock(name, patch)
        else:
            logger.warning(
                "PSAPCluster %s: failed to check sentinel job: %s", name, exc
            )
