"""Execution handlers — reconcile_admitted and reconcile_running.

Covers the later phases of a FournosJob: creating and monitoring
the Tekton PipelineRun.
"""

from __future__ import annotations

import logging

from kubernetes import client

from fournos.core.constants import Phase
from fournos.core.tekton import TektonClient
from fournos.settings import settings
from fournos.state import ctx

from .status import (
    COND_PIPELINE_RUN_READY,
    COND_WORKLOAD_ADMITTED,
    owner_ref,
    set_condition,
)

logger = logging.getLogger(__name__)


def handle_abort(name, status, patch):
    """Start aborting a job.

    If a PipelineRun exists, cancel it and transition to Aborting — the
    Workload (and its quota) is kept until the PipelineRun finishes its
    ``finally`` cleanup tasks.  If no PipelineRun exists (Pending phase),
    delete the Workload immediately and go straight to Aborted.
    """
    phase = status.get("phase", "")
    conditions = list(status.get("conditions") or [])

    has_pipeline_run = phase in (Phase.RUNNING, Phase.ADMITTED)
    if has_pipeline_run:
        ctx.tekton.cancel_pipeline_run(name)
        patch.status["phase"] = Phase.ABORTING
        patch.status["message"] = "Abort requested, waiting for PipelineRun cleanup"
        set_condition(
            patch,
            conditions,
            COND_PIPELINE_RUN_READY,
            "False",
            "Aborting",
            "PipelineRun cancellation requested (CancelledRunFinally)",
        )
        logger.info("Job %s: cancellation sent, phase=Aborting (was %s)", name, phase)
    else:
        ctx.kueue.delete_workload(name)
        patch.status["phase"] = Phase.ABORTED
        patch.status["message"] = "Job aborted by user"
        set_condition(
            patch,
            conditions,
            COND_WORKLOAD_ADMITTED,
            "False",
            "Aborted",
            "Job aborted by user",
        )
        logger.info("Job %s: aborted (was %s)", name, phase)


def reconcile_aborting(name, status, patch):
    """Poll a cancelled PipelineRun until it finishes, then complete the abort."""
    pr = ctx.tekton.get_pipeline_run_or_none(name)
    conditions = list(status.get("conditions") or [])

    if pr is None:
        _finish_abort(name, conditions, patch, "PipelineRun not found")
        return

    pr_status, pr_message = TektonClient.extract_status(pr)
    logger.info(
        "Job %s: aborting, PipelineRun status=%s, message=%s",
        name,
        pr_status,
        pr_message,
    )

    if pr_status in ("succeeded", "failed"):
        _finish_abort(name, conditions, patch, pr_message)
    else:
        new_msg = (
            f"Aborting, waiting for cleanup: {pr_message}"
            if pr_message
            else "Aborting, waiting for PipelineRun cleanup"
        )
        if status.get("message") != new_msg:
            patch.status["message"] = new_msg


def _finish_abort(name, conditions, patch, pr_message):
    """Transition from Aborting to Aborted: delete Workload and set terminal status."""
    ctx.kueue.delete_workload(name)

    patch.status["phase"] = Phase.ABORTED
    patch.status["message"] = "Job aborted by user"

    set_condition(
        patch,
        conditions,
        COND_WORKLOAD_ADMITTED,
        "False",
        "Aborted",
        "Job aborted by user",
    )
    set_condition(
        patch,
        patch.status["conditions"],
        COND_PIPELINE_RUN_READY,
        "False",
        "Aborted",
        pr_message or "Job aborted by user",
    )
    logger.info("Job %s: abort complete, phase=Aborted", name)


def reconcile_admitted(spec, name, namespace, status, patch, body):
    pr = ctx.tekton.get_pipeline_run_or_none(name)
    conditions = list(status.get("conditions") or [])

    if pr is None:
        cluster = status.get("cluster", "")
        secret = ctx.registry.resolve_kubeconfig_secret(cluster)
        hardware = spec.get("hardware")
        gpu_count = hardware.get("gpuCount", 0) if hardware else 0

        display_name = spec.get("displayName") or name

        try:
            ctx.tekton.create_pipeline_run(
                name=name,
                display_name=display_name,
                pipeline=spec.get("pipeline", "fournos-full"),
                forge_project=spec["forge"]["project"],
                forge_config=spec["forge"],
                env=spec.get("env", {}),
                kubeconfig_secret=secret,
                gpu_count=gpu_count,
                secret_refs=spec.get("secretRefs", []),
                cluster=cluster,
                owner_ref=owner_ref(body),
            )
            logger.info(
                "Job %s: created PipelineRun for target cluster %s", name, cluster
            )
        except client.exceptions.ApiException as exc:
            if exc.status != 409:
                patch.status["phase"] = Phase.FAILED
                patch.status["message"] = f"Failed to create PipelineRun: {exc.reason}"
                set_condition(
                    patch,
                    conditions,
                    COND_PIPELINE_RUN_READY,
                    "False",
                    "CreateFailed",
                    f"Failed to create PipelineRun: {exc.reason}",
                )
                ctx.kueue.delete_workload(name)
                logger.error(
                    "Job %s: PipelineRun creation failed (HTTP %s): %s",
                    name,
                    exc.status,
                    exc.reason,
                )
                return
            logger.info("Job %s: PipelineRun already exists (409), proceeding", name)

    patch.status["phase"] = Phase.RUNNING
    patch.status["pipelineRun"] = name
    patch.status["message"] = "PipelineRun created, waiting for execution"
    set_condition(
        patch,
        conditions,
        COND_PIPELINE_RUN_READY,
        "Unknown",
        "Started",
        "PipelineRun has been created",
    )
    if settings.tekton_dashboard_url:
        base = settings.tekton_dashboard_url.rstrip("/")
        patch.status["dashboardURL"] = (
            f"{base}/#/namespaces/{namespace}/pipelineruns/{name}"
        )


def reconcile_running(name, status, patch):
    pr = ctx.tekton.get_pipeline_run_or_none(name)
    conditions = list(status.get("conditions") or [])

    if pr is None:
        patch.status["phase"] = Phase.FAILED
        patch.status["message"] = "PipelineRun not found"
        set_condition(
            patch,
            conditions,
            COND_PIPELINE_RUN_READY,
            "False",
            "NotFound",
            f"PipelineRun {name} not found",
        )
        ctx.kueue.delete_workload(name)
        logger.error("Job %s: PipelineRun %s not found", name, name)
        return

    pr_status, pr_message = TektonClient.extract_status(pr)
    logger.info(
        "Job %s: PipelineRun status=%s, message=%s", name, pr_status, pr_message
    )
    if pr_status == "succeeded":
        patch.status["phase"] = Phase.SUCCEEDED
        patch.status["message"] = "Pipeline completed successfully"
        set_condition(
            patch,
            conditions,
            COND_PIPELINE_RUN_READY,
            "True",
            "Succeeded",
            pr_message or "Pipeline completed successfully",
        )
        ctx.kueue.delete_workload(name)
        logger.info("Job %s: succeeded", name)
    elif pr_status == "failed":
        patch.status["phase"] = Phase.FAILED
        patch.status["message"] = pr_message or "PipelineRun failed"
        set_condition(
            patch,
            conditions,
            COND_PIPELINE_RUN_READY,
            "False",
            "Failed",
            pr_message or "PipelineRun failed",
        )
        ctx.kueue.delete_workload(name)
        logger.warning("Job %s: PipelineRun failed: %s", name, pr_message)
    else:
        new_msg = (
            f"Pipeline running: {pr_message}" if pr_message else "Pipeline running"
        )
        if status.get("message") != new_msg:
            patch.status["message"] = new_msg
            set_condition(
                patch,
                conditions,
                COND_PIPELINE_RUN_READY,
                "Unknown",
                "Running",
                pr_message or "PipelineRun is executing",
            )
