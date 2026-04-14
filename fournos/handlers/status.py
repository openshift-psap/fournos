"""Status utilities — condition helpers, owner references, and shared constants."""

from __future__ import annotations

import datetime

CRD_GROUP = "fournos.dev"
CRD_VERSION = "v1"

COND_WORKLOAD_ADMITTED = "WorkloadAdmitted"
COND_PIPELINE_RUN_READY = "PipelineRunReady"


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
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def create_workload_for_job(spec, name, body):
    """Create a Kueue Workload with cluster-slot reservation."""
    from fournos.state import ctx

    hardware = spec.get("hardware")
    ctx.kueue.create_workload(
        name=name,
        gpu_type=hardware.get("gpuType") if hardware else None,
        gpu_count=hardware.get("gpuCount", 0) if hardware else 0,
        cluster=spec.get("cluster"),
        exclusive=spec.get("exclusive", False),
        priority=spec.get("priority"),
        owner_ref=owner_ref(body),
    )
