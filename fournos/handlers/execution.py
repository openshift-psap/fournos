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
    owner_ref,
    set_condition,
)

logger = logging.getLogger(__name__)


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
