"""Execution handlers — reconcile_admitted and reconcile_running.

Covers the later phases of a FournosJob: creating and monitoring
the Tekton PipelineRun.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from kubernetes import client

from fournos.core.constants import Phase, Shutdown
from fournos.core.tekton import (
    ANNOTATION_PREEMPTION_CLEANUP_TASK,
    PREEMPTION_CLEANUP_RESULT,
    PREEMPTION_CLEANUP_SERVICE_ACCOUNT,
    TektonClient,
)
from fournos.settings import settings
from fournos.state import ctx

from .lifecycle import is_lock_only, parse_iso_timestamp
from .status import (
    COND_PIPELINE_RUN_READY,
    COND_WORKLOAD_ADMITTED,
    owner_ref,
    set_condition,
    set_terminal_phase,
)

logger = logging.getLogger(__name__)


def handle_eviction(name, status, patch, eviction):
    """Stop an evicted job without releasing its Kueue quota yet.

    Kueue keeps the Workload admitted while its owner stops execution.  The
    Stopping phase and stopReason survive operator restarts; the reservation
    is released only by reconcile_stopping after cleanup has been verified.
    """
    reason = "Preempted" if eviction.get("reason") == "Preempted" else "Evicted"
    pr = ctx.tekton.get_pipeline_run_or_none(name)
    conditions = list(status.get("conditions") or [])

    if pr is not None:
        patch.status["pipelineRun"] = name
        pr_status, _ = TektonClient.extract_status(pr)
        if (
            pr_status == "running"
            and not pr.get("status", {}).get("completionTime")
            and pr.get("spec", {}).get("status")
            not in ("Cancelled", "CancelledRunFinally")
        ):
            ctx.tekton.cancel_pipeline_run(name, graceful=True)
        message = f"{reason} by Kueue, waiting for PipelineRun cleanup"
    else:
        # A Running job whose PipelineRun disappeared is not proof that its
        # remote resources are gone.  Preserve that fact for reconciliation.
        if status.get("phase") == Phase.RUNNING:
            patch.status["pipelineRun"] = name
        message = f"{reason} by Kueue, checking whether execution started"

    patch.status["phase"] = Phase.STOPPING
    patch.status["stopReason"] = reason
    patch.status["message"] = message
    if (
        pr is not None
        or status.get("pipelineRun")
        or status.get("phase") == Phase.RUNNING
    ):
        set_condition(
            patch,
            conditions,
            COND_PIPELINE_RUN_READY,
            "False",
            Phase.STOPPING,
            message,
        )
    logger.info("Job %s: Kueue %s, phase=Stopping", name, reason.lower())


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
        set_terminal_phase(patch, Phase.STOPPED, "Job stopped by user")
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
    if status.get("stopReason") in ("Preempted", "Evicted"):
        _reconcile_evicted_stopping(name, status, patch)
        return

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


def _reconcile_evicted_stopping(name, status, patch):
    """Hold quota until a cancelled PipelineRun's cleanup is proven complete."""
    pr = ctx.tekton.get_pipeline_run_or_none(name)
    if pr is None:
        if status.get("pipelineRun"):
            _hold_evicted_workload(
                name,
                status,
                patch,
                "PipelineRun is missing; remote cleanup cannot be verified",
            )
            return
        _finish_eviction(name, status, patch, had_pipeline_run=False)
        return

    if not pr.get("status", {}).get("completionTime"):
        patch.status["pipelineRun"] = name
        if pr.get("spec", {}).get("status") not in (
            "Cancelled",
            "CancelledRunFinally",
        ):
            ctx.tekton.cancel_pipeline_run(name, graceful=True)
        _, pr_message = TektonClient.extract_status(pr)
        message = f"{status['stopReason']} by Kueue, waiting for PipelineRun cleanup"
        if pr_message:
            message = f"{message}: {pr_message}"
        if status.get("message") != message:
            patch.status["message"] = message
        return

    try:
        cleanup_verified, detail = _preemption_cleanup_verified(pr)
    except client.exceptions.ApiException as exc:
        _hold_evicted_workload(
            name, status, patch, f"Could not read cleanup TaskRun: {exc.reason}"
        )
        return

    if not cleanup_verified:
        _hold_evicted_workload(name, status, patch, detail)
        return

    _finish_eviction(name, status, patch, had_pipeline_run=True)


def _preemption_cleanup_verified(pr):
    """Require a successful, completed TaskRun named by this run's contract."""
    task_name = (pr.get("metadata", {}).get("annotations") or {}).get(
        ANNOTATION_PREEMPTION_CLEANUP_TASK
    )
    if not task_name:
        return False, "PipelineRun has no preemption cleanup task contract"
    if (
        pr.get("spec", {}).get("taskRunTemplate", {}).get("serviceAccountName")
        != PREEMPTION_CLEANUP_SERVICE_ACCOUNT
    ):
        return False, "PipelineRun did not use the restricted cleanup service account"

    references = [
        ref
        for ref in pr.get("status", {}).get("childReferences", [])
        if ref.get("pipelineTaskName") == task_name
        and ref.get("kind", "TaskRun") == "TaskRun"
        and ref.get("name")
    ]
    if not references:
        return False, f"Cleanup task {task_name} has no TaskRun"

    for ref in references:
        task_run = ctx.tekton.get_task_run_or_none(ref["name"])
        if task_run is None or not task_run.get("status", {}).get("completionTime"):
            continue
        if any(
            condition.get("type") == "Succeeded" and condition.get("status") == "True"
            for condition in task_run.get("status", {}).get("conditions", [])
        ) and any(
            result.get("name") == PREEMPTION_CLEANUP_RESULT
            and result.get("value") == "true"
            for result in task_run.get("status", {}).get("results", [])
        ):
            return True, ""

    return False, f"Cleanup task {task_name} did not verify remote cleanup"


def _hold_evicted_workload(name, status, patch, detail):
    """Surface a cleanup problem while keeping the Kueue reservation."""
    message = (
        f"{status['stopReason']} by Kueue; {detail}. Workload reservation retained"
    )
    if status.get("message") == message:
        return
    patch.status["message"] = message
    set_condition(
        patch,
        list(status.get("conditions") or []),
        COND_PIPELINE_RUN_READY,
        "False",
        "CleanupUnverified",
        detail,
    )
    logger.warning("Job %s: %s", name, message)


def _finish_eviction(name, status, patch, *, had_pipeline_run):
    """Release the victim's quota after its remote execution has stopped."""
    reason = status["stopReason"]
    message = f"Job {reason.lower()} by Kueue"
    ctx.kueue.delete_workload(name)
    set_terminal_phase(patch, Phase.STOPPED, message)
    conditions = list(status.get("conditions") or [])
    set_condition(
        patch,
        conditions,
        COND_WORKLOAD_ADMITTED,
        "False",
        reason,
        message,
    )
    if had_pipeline_run:
        set_condition(
            patch,
            patch.status["conditions"],
            COND_PIPELINE_RUN_READY,
            "False",
            reason,
            "PipelineRun stopped after verified cleanup",
        )
    logger.info("Job %s: %s, phase=Stopped", name, reason.lower())


def _finish_stop(name, conditions, patch, pr_message):
    """Transition from Stopping to Stopped: delete Workload and set terminal status."""
    ctx.kueue.delete_workload(name)

    set_terminal_phase(patch, Phase.STOPPED, "Job stopped by user")

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
    if is_lock_only(spec):
        # lockUntil was already validated in on_create, so a parse failure
        # here shouldn't happen; treat it as "no deadline" rather than crash
        # the reconcile loop.
        try:
            lock_until = parse_iso_timestamp(spec.get("lockUntil"), "lockUntil")
        except ValueError:
            logger.debug("Job %s: unexpected invalid lockUntil in spec", name)
            lock_until = None
        if lock_until is not None and datetime.now(UTC) >= lock_until:
            logger.info("Job %s: lockUntil reached (%s), releasing", name, lock_until)
            handle_shutdown(name, status, patch, Shutdown.TERMINATE)
            return

        cluster = status.get("cluster", spec.get("cluster", ""))
        new_msg = f"Cluster lock held on {cluster}"
        if status.get("message") != new_msg:
            patch.status["message"] = new_msg
        return

    pr = ctx.tekton.get_pipeline_run_or_none(name)
    conditions = list(status.get("conditions") or [])

    if pr is None:
        clusterless = spec.get("clusterless", False)

        if clusterless:
            # For clusterless jobs, skip kubeconfig handling
            kubeconfig_secret = None
            cluster = ""
        else:
            cluster = status.get("cluster", "")
            try:
                kubeconfig_secret = ctx.registry.copy_kubeconfig_secret(
                    cluster, name, owner_ref(body)
                )
            except client.exceptions.ApiException as exc:
                set_terminal_phase(
                    patch, Phase.FAILED, f"Failed to copy kubeconfig: {exc.reason}"
                )
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
            set_terminal_phase(patch, Phase.FAILED, msg)
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
            forge_engine = (spec.get("executionEngine") or {}).get("forge") or {}
            engine_project = (
                forge_engine.get("project") if isinstance(forge_engine, dict) else None
            )
            # A PR checkout can change how it creates target resources.  Its
            # cleanup cannot claim the deployed-image verification contract.
            job_env = spec.get("env") or {}
            allow_cleanup_contract = not (
                job_env.get("PULL_PULL_SHA") or job_env.get("PULL_NUMBER")
            )
            ctx.tekton.create_pipeline_run(
                name=name,
                pipeline=spec.get("pipeline", "fournos-full"),
                kubeconfig_secret=kubeconfig_secret,
                resolved_secrets=resolved_secrets,
                cluster=cluster,
                engine_project=engine_project,
                allow_cleanup_contract=allow_cleanup_contract,
                owner_ref=owner_ref(body),
            )
            if clusterless:
                logger.info("Job %s: created PipelineRun in clusterless mode", name)
            else:
                logger.info(
                    "Job %s: created PipelineRun for target cluster %s", name, cluster
                )
        except ValueError as exc:
            set_terminal_phase(patch, Phase.FAILED, str(exc))
            set_condition(
                patch,
                conditions,
                COND_PIPELINE_RUN_READY,
                "False",
                "InvalidCleanupContract",
                str(exc),
            )
            ctx.kueue.delete_workload(name)
            logger.error("Job %s: invalid Pipeline cleanup contract: %s", name, exc)
            return
        except client.exceptions.ApiException as exc:
            if exc.status != 409:
                set_terminal_phase(
                    patch, Phase.FAILED, f"Failed to create PipelineRun: {exc.reason}"
                )
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
        set_terminal_phase(patch, Phase.FAILED, "PipelineRun not found")
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
        set_terminal_phase(patch, Phase.SUCCEEDED, "Pipeline completed successfully")
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
        set_terminal_phase(patch, Phase.FAILED, pr_message or "PipelineRun failed")
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
