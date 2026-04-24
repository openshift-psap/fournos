"""Resolving handler — reconcile_resolving.

Covers the Resolving phase: launching a Forge resolve Job, reading
its FournosJobConfig output, validating the results, and creating
the Kueue Workload to transition into Pending.
"""

from __future__ import annotations

import logging

from kubernetes import client

from fournos.core.constants import Phase
from fournos.core.resolve import ResolveClient
from fournos.state import ctx

from .status import (
    COND_RESOLVED,
    COND_WORKLOAD_ADMITTED,
    owner_ref,
    set_condition,
)

logger = logging.getLogger(__name__)


def _resolve_failed(patch, conditions, name, message, *, reason, cond_message=None):
    """Set phase=Failed with a Resolved=False condition."""
    patch.status["phase"] = Phase.FAILED
    patch.status["message"] = message
    set_condition(
        patch,
        conditions,
        COND_RESOLVED,
        "False",
        reason,
        cond_message or message,
    )
    logger.error("Job %s: %s", name, message)


# ---------------------------------------------------------------------------
# Sub-steps
# ---------------------------------------------------------------------------


def _ensure_resolve_job(spec, name, conditions, patch, body):
    """Create the resolve Job if it doesn't exist yet.

    Returns the existing Job dict, or None if the Job was just created
    (or a 409 race was hit) — the caller should return and wait for the
    next reconcile tick.  Returns ``False`` on fatal creation failure
    (patch already set to Failed).
    """
    job = ctx.resolve.get_job_or_none(name)
    if job is not None:
        return job

    try:
        ctx.resolve.create_job(
            name=name,
            forge_project=spec["forge"]["project"],
            forge_config=spec["forge"],
            env=spec.get("env", {}),
            owner_ref=owner_ref(body),
        )
    except client.exceptions.ApiException as exc:
        if exc.status == 409:
            logger.info("Job %s: resolve Job already exists (409)", name)
            return None
        _resolve_failed(
            patch,
            conditions,
            name,
            f"Failed to create resolve Job: {exc.reason}",
            reason="CreateFailed",
        )
        return False

    set_condition(
        patch,
        conditions,
        COND_RESOLVED,
        "False",
        "Resolving",
        "Resolve Job created, waiting for completion",
    )
    patch.status["message"] = "Resolving job requirements via Forge"
    logger.info("Job %s: created resolve Job", name)
    return None


def _check_job_finished(job, name, conditions, patch):
    """Check whether the resolve Job has finished.

    Returns ``True`` if the Job succeeded.  Returns ``None`` if still
    running.  Returns ``False`` and sets Failed on the patch if the
    Job failed.
    """
    job_status = ResolveClient.get_job_status(job)

    if job_status == "running":
        logger.info("Job %s: resolve Job still running", name)
        return None

    if job_status == "failed":
        message = ResolveClient.get_job_message(job) or "Resolve Job failed"
        _resolve_failed(
            patch,
            conditions,
            name,
            f"Forge resolution failed: {message}",
            reason="Failed",
            cond_message=message,
        )
        return False

    return True


def _read_config(name, conditions, patch):
    """Read the FournosJobConfig after a successful resolve Job.

    Returns the config ``spec`` dict, or ``None`` if the config is missing
    (patch already set to Failed).
    """
    config = ctx.resolve.read_job_config(name)
    if config is None:
        _resolve_failed(
            patch,
            conditions,
            name,
            "FournosJobConfig not found (may have been deleted externally)",
            reason="ConfigMissing",
            cond_message="FournosJobConfig not found after resolve Job succeeded",
        )
    return config


def _resolve_hardware(spec, config, name, conditions, patch):
    """Determine and validate GPU requirements from spec or config.

    User-provided ``spec.hardware`` takes precedence.  The GPU type is
    always validated against Kueue regardless of source.

    Returns ``(gpu_type, gpu_count)`` on success, or ``None`` if
    validation failed (patch already set to Failed).
    """
    hardware = spec.get("hardware")
    if hardware:
        gpu_type = hardware.get("gpuType")
        gpu_count = hardware.get("gpuCount", 0)
    else:
        config_hw = config.get("hardware") or {}
        gpu_type = config_hw.get("gpuType")
        gpu_count = config_hw.get("gpuCount", 0)

    if not gpu_type or not gpu_count:
        _resolve_failed(
            patch,
            conditions,
            name,
            "No hardware requirements: neither spec.hardware nor "
            "FournosJobConfig provides gpuType and gpuCount",
            reason="NoHardware",
            cond_message="No hardware requirements found",
        )
        return None

    try:
        known_gpu_types = ctx.kueue.list_gpu_types()
    except client.exceptions.ApiException as exc:
        _resolve_failed(
            patch,
            conditions,
            name,
            f"Failed to list GPU types: {exc.reason}",
            reason="InvalidGPUType",
            cond_message=f"Kueue API error: {exc.reason}",
        )
        return None
    if not known_gpu_types:
        _resolve_failed(
            patch,
            conditions,
            name,
            "No GPU types configured",
            reason="InvalidGPUType",
            cond_message="No GPU types found in any ClusterQueue",
        )
        logger.error("Job %s: no GPU types found in any ClusterQueue", name)
        return None
    if gpu_type not in known_gpu_types:
        _resolve_failed(
            patch,
            conditions,
            name,
            f"GPU type '{gpu_type}' not available. "
            f"Valid types: {', '.join(sorted(known_gpu_types))}",
            reason="InvalidGPUType",
            cond_message=f"GPU type '{gpu_type}' not available",
        )
        return None

    return gpu_type, gpu_count


def _validate_secret_refs(config, name, conditions, patch):
    """Validate secretRefs from the FournosJobConfig against Vault secrets.

    Returns ``True`` on success (including when there are no secretRefs),
    or ``False`` if validation failed (patch already set to Failed).
    """
    secret_refs = config.get("secretRefs") or []
    if not secret_refs:
        return True

    try:
        ctx.registry.resolve_secret_refs(secret_refs)
    except KeyError as exc:
        msg = str(exc).strip("'\"")
        _resolve_failed(
            patch,
            conditions,
            name,
            msg,
            reason="SecretRefNotFound",
        )
        return False

    return True


def _create_workload_and_transition(
    spec, name, conditions, patch, body, gpu_type, gpu_count
):
    """Create the Kueue Workload and transition to Pending."""
    try:
        ctx.kueue.create_workload(
            name=name,
            gpu_type=gpu_type,
            gpu_count=gpu_count,
            cluster=spec.get("cluster"),
            exclusive=spec.get("exclusive", False),
            priority=spec.get("priority"),
            owner_ref=owner_ref(body),
        )
    except client.exceptions.ApiException as exc:
        if exc.status == 409:
            pass
        else:
            _resolve_failed(
                patch,
                conditions,
                name,
                f"Failed to create Workload: {exc.reason}",
                reason="WorkloadFailed",
                cond_message=f"Workload creation failed: {exc.reason}",
            )
            return

    set_condition(
        patch,
        conditions,
        COND_RESOLVED,
        "True",
        "Resolved",
        "Forge resolution complete",
    )
    set_condition(
        patch,
        patch.status["conditions"],
        COND_WORKLOAD_ADMITTED,
        "False",
        "Pending",
        "Workload created, waiting for Kueue admission",
    )
    patch.status["phase"] = Phase.PENDING
    patch.status["message"] = "Workload created, waiting for Kueue admission"
    logger.info("Job %s: resolved, created Workload, phase=Pending", name)


# ---------------------------------------------------------------------------
# Top-level reconciler
# ---------------------------------------------------------------------------


def reconcile_resolving(spec, name, status, patch, body):
    conditions = list(status.get("conditions") or [])

    ctx.resolve.create_fournos_job_config(name=name, owner_ref=owner_ref(body))

    job = _ensure_resolve_job(spec, name, conditions, patch, body)
    if job is None or job is False:
        return

    if not _check_job_finished(job, name, conditions, patch):
        return

    config = _read_config(name, conditions, patch)
    if config is None:
        return

    hw = _resolve_hardware(spec, config, name, conditions, patch)
    if hw is None:
        return

    if not _validate_secret_refs(config, name, conditions, patch):
        return

    gpu_type, gpu_count = hw
    _create_workload_and_transition(
        spec, name, conditions, patch, body, gpu_type, gpu_count
    )
