"""Status utilities — condition helpers, owner references, and shared constants."""

from __future__ import annotations

import datetime
import logging

from fournos.core.constants import TERMINAL_PHASES, Phase

CRD_GROUP = "fournos.dev"
CRD_VERSION = "v1"

COND_RESOLVED = "Resolved"
COND_WORKLOAD_ADMITTED = "WorkloadAdmitted"
COND_PIPELINE_RUN_READY = "PipelineRunReady"

logger = logging.getLogger(__name__)


def owner_ref(body: dict) -> dict:
    """Build a Kubernetes ownerReference pointing at the given FournosJob."""
    return {
        "apiVersion": f"{CRD_GROUP}/{CRD_VERSION}",
        "kind": "FournosJob",
        "name": body["metadata"]["name"],
        "uid": body["metadata"]["uid"],
        "controller": True,
        "blockOwnerDeletion": True,
    }


def utcnow() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def set_condition(
    patch,
    existing_conditions: list[dict],
    type_: str,
    cond_status: str,
    reason: str,
    message: str,
) -> None:
    """Upsert a condition by type, preserving lastTransitionTime when status is unchanged."""
    now = utcnow()
    old = next((c for c in existing_conditions if c.get("type") == type_), None)

    if old and old.get("status") == cond_status:
        transition_time = old.get("lastTransitionTime", now)
    else:
        transition_time = now

    new_cond: dict = {
        "type": type_,
        "status": cond_status,
        "lastTransitionTime": transition_time,
    }
    if reason:
        new_cond["reason"] = reason
    if message:
        new_cond["message"] = message

    result = [c for c in existing_conditions if c.get("type") != type_]
    result.append(new_cond)
    patch.status["conditions"] = result


def set_terminal_phase(patch, phase: str, message: str) -> None:
    """Set phase, message, and completionTime for a terminal transition."""
    if phase not in TERMINAL_PHASES:
        logger.error("set_terminal_phase called with non-terminal phase %r", phase)
        message = f"{message} (internal error: {phase!r} is not a terminal phase)"
        phase = Phase.FAILED
    patch.status["phase"] = phase
    patch.status["message"] = message
    patch.status["completionTime"] = utcnow()


def create_workload_for_job(spec, name, body):
    """Create a Kueue Workload with cluster-slot reservation."""
    from fournos.state import ctx

    hardware = spec.get("hardware")
    ctx.kueue.create_workload(
        name=name,
        gpu_type=hardware.get("gpuType") if hardware else None,
        gpu_count=hardware.get("gpuCount", 0) if hardware else 0,
        cluster=spec.get("cluster"),
        gpu_pool=spec.get("gpuPool"),
        exclusive=spec["exclusive"],
        priority=spec.get("priority"),
        owner_ref=owner_ref(body),
    )
