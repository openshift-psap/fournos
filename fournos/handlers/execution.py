"""Execution handlers — reconcile_admitted and reconcile_running.

Covers the later phases of a FournosJob: creating and monitoring
the Tekton PipelineRun.
"""

from __future__ import annotations

import logging

from kubernetes import client

from fournos.core.constants import Phase, Shutdown
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


def handle_shutdown(name, status, patch, shutdown):
    """Start shutting down a job.

    If a PipelineRun exists, cancel it and transition to Stopping — the
    Workload (and its quota) is kept until the PipelineRun finishes.
    ``Stop`` uses CancelledRunFinally (runs finally tasks), ``Terminate``
    uses Cancelled (skips finally tasks).  If no PipelineRun exists
    (Pending phase), delete the Workload immediately and go straight to
    Stopped.
    """
    phase = status.get("phase", "")
    conditions = list(status.get("conditions") or [])

    pr = (
        ctx.tekton.get_pipeline_run_or_none(name)
        if phase in (Phase.RUNNING, Phase.ADMITTED)
        else None
    )
    if pr is not None:
        graceful = shutdown == Shutdown.STOP
        ctx.tekton.cancel_pipeline_run(name, graceful=graceful)

        patch.status["phase"] = Phase.STOPPING
        if graceful:
            patch.status["message"] = (
                f"Shutdown ({shutdown}) requested, waiting for PipelineRun cleanup"
            )
        else:
            patch.status["message"] = (
                f"Shutdown ({shutdown}) requested, waiting for PipelineRun to stop"
            )
        set_condition(
            patch,
            conditions,
            COND_PIPELINE_RUN_READY,
            "False",
            Phase.STOPPING,
            f"PipelineRun cancellation requested (graceful={graceful})",
        )
        logger.info(
            "Job %s: %s sent (graceful=%s), phase=Stopping (was %s)",
            name,
            shutdown,
            graceful,
            phase,
        )
    else:
        ctx.kueue.delete_workload(name)
        patch.status["phase"] = Phase.STOPPED
        patch.status["message"] = "Job stopped by user"
        set_condition(
            patch,
            conditions,
            COND_WORKLOAD_ADMITTED,
            "False",
            Phase.STOPPED,
            "Job stopped by user",
        )
        logger.info("Job %s: stopped (was %s)", name, phase)


def reconcile_stopping(name, status, patch):
    """Poll a cancelled PipelineRun until it finishes, then complete shutdown."""
    pr = ctx.tekton.get_pipeline_run_or_none(name)
    conditions = list(status.get("conditions") or [])

    if pr is None:
        _finish_stop(name, conditions, patch, "PipelineRun not found")
        return

    pr_status, pr_message = TektonClient.extract_status(pr)
    logger.info(
        "Job %s: stopping, PipelineRun status=%s, message=%s",
        name,
        pr_status,
        pr_message,
    )

    if pr_status in ("succeeded", "failed"):
        _finish_stop(name, conditions, patch, pr_message)
    else:
        new_msg = (
            f"Stopping, waiting for cleanup: {pr_message}"
            if pr_message
            else "Stopping, waiting for PipelineRun cleanup"
        )
        if status.get("message") != new_msg:
            patch.status["message"] = new_msg


def _finish_stop(name, conditions, patch, pr_message):
    """Transition from Stopping to Stopped: delete Workload and set terminal status."""
    ctx.kueue.delete_workload(name)

    patch.status["phase"] = Phase.STOPPED
    patch.status["message"] = "Job stopped by user"

    set_condition(
        patch,
        conditions,
        COND_WORKLOAD_ADMITTED,
        "False",
        Phase.STOPPED,
        "Job stopped by user",
    )
    set_condition(
        patch,
        patch.status["conditions"],
        COND_PIPELINE_RUN_READY,
        "False",
        Phase.STOPPED,
        pr_message or "Job stopped by user",
    )
    logger.info("Job %s: stopped, phase=Stopped", name)


def reconcile_admitted(spec, name, namespace, status, patch, body):
    if spec.get("lockOnly", False):
        cluster = status.get("cluster", spec.get("cluster", ""))
        new_msg = f"Cluster lock held on {cluster}"
        if status.get("message") != new_msg:
            patch.status["message"] = new_msg
        return

    pr = ctx.tekton.get_pipeline_run_or_none(name)
    conditions = list(status.get("conditions") or [])

    if pr is None:
        cluster = status.get("cluster", "")

        try:
            kubeconfig_secret = ctx.registry.copy_kubeconfig_secret(
                cluster, name, owner_ref(body)
            )
        except client.exceptions.ApiException as exc:
            patch.status["phase"] = Phase.FAILED
            patch.status["message"] = f"Failed to copy kubeconfig: {exc.reason}"
            set_condition(
                patch,
                conditions,
                COND_PIPELINE_RUN_READY,
                "False",
                "KubeconfigNotFound",
                f"Failed to copy kubeconfig: {exc.reason}",
            )
            ctx.kueue.delete_workload(name)
            logger.error("Job %s: kubeconfig copy failed: %s", name, exc)
            return

        secret_refs_raw = spec.get("secretRefs") or []
        try:
            resolved_secrets = ctx.registry.copy_secrets(
                secret_refs_raw, name, owner_ref(body)
            )
        except (KeyError, client.exceptions.ApiException) as exc:
            msg = str(exc).strip("'\"") if isinstance(exc, KeyError) else exc.reason
            patch.status["phase"] = Phase.FAILED
            patch.status["message"] = msg
            set_condition(
                patch,
                conditions,
                COND_PIPELINE_RUN_READY,
                "False",
                "SecretRefNotFound",
                msg,
            )
            ctx.kueue.delete_workload(name)
            logger.error("Job %s: %s", name, exc)
            return

        try:
            ctx.tekton.create_pipeline_run(
                name=name,
                pipeline=spec.get("pipeline", "fournos-full"),
                kubeconfig_secret=kubeconfig_secret,
                resolved_secrets=resolved_secrets,
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
