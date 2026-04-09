"""Fournos Kubernetes operator — watches FournosJob CRs and orchestrates
Kueue Workloads + Tekton PipelineRuns."""

from __future__ import annotations

import datetime
import logging
import threading
import time

import kopf
from kubernetes import client, config

from fournos.core.clusters import ClusterRegistry
from fournos.core.constants import LABEL_JOB_NAME
from fournos.core.kueue import KueueClient
from fournos.core.tekton import TektonClient
from fournos.settings import settings

logger = logging.getLogger(__name__)

_kueue: KueueClient
_tekton: TektonClient
_registry: ClusterRegistry

COND_WORKLOAD_ADMITTED = "WorkloadAdmitted"
COND_PIPELINE_RUN_READY = "PipelineRunReady"


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _set_condition(
    patch,
    existing_conditions: list[dict],
    type_: str,
    cond_status: str,
    reason: str,
    message: str,
) -> None:
    """Upsert a condition by type, preserving lastTransitionTime when status is unchanged."""
    now = _utcnow()
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


@kopf.on.startup()
def startup(**_):
    global _kueue, _tekton, _registry

    logging.getLogger("fournos").setLevel(settings.log_level.upper())

    try:
        config.load_incluster_config()
        logger.info("Loaded in-cluster Kubernetes config")
    except config.ConfigException:
        config.load_kube_config()
        logger.info("Loaded local kubeconfig")

    custom_objects = client.CustomObjectsApi()
    _kueue = KueueClient(custom_objects)
    _tekton = TektonClient(custom_objects)
    _registry = ClusterRegistry(client.CoreV1Api())

    gc_thread = threading.Thread(target=_gc_loop, daemon=True)
    gc_thread.start()
    logger.info("Resource GC started (interval=%ss)", settings.gc_interval_sec)


# ---------------------------------------------------------------------------
# CREATE / RESUME — validate spec, create Kueue Workload, set phase=Pending
# ---------------------------------------------------------------------------


@kopf.on.create("fournos.dev", "v1", "fournosjobs")
@kopf.on.resume("fournos.dev", "v1", "fournosjobs")
def on_create(spec, name, namespace, status, patch, **_):
    if status.get("phase"):
        return  # Already initialised (resume of existing CR)

    cluster = spec.get("cluster")
    hardware = spec.get("hardware")

    if not cluster and not hardware:
        patch.status["phase"] = "Failed"
        patch.status["message"] = "Must specify 'cluster', 'hardware', or both"
        return

    if cluster:
        try:
            known_flavors = _kueue.list_flavors()
        except client.exceptions.ApiException as exc:
            patch.status["phase"] = "Failed"
            patch.status["message"] = f"Failed to list clusters: {exc.reason}"
            logger.error("Job %s: list_flavors failed: %s", name, exc.reason)
            return
        if cluster not in known_flavors:
            patch.status["phase"] = "Failed"
            patch.status["message"] = f"Cluster '{cluster}' not found"
            return

    if hardware and hardware.get("gpuType"):
        gpu_type = hardware["gpuType"]
        try:
            known_gpu_types = _kueue.list_gpu_types()
        except client.exceptions.ApiException as exc:
            patch.status["phase"] = "Failed"
            patch.status["message"] = f"Failed to list GPU types: {exc.reason}"
            logger.error("Job %s: list_gpu_types failed: %s", name, exc.reason)
            return
        if known_gpu_types and gpu_type not in known_gpu_types:
            patch.status["phase"] = "Failed"
            patch.status["message"] = (
                f"GPU type '{gpu_type}' not available. "
                f"Valid types: {', '.join(sorted(known_gpu_types))}"
            )
            return

    try:
        _kueue.create_workload(
            name=name,
            gpu_type=hardware.get("gpuType") if hardware else None,
            gpu_count=hardware.get("gpuCount", 0) if hardware else 0,
            cluster=cluster,
            priority=spec.get("priority"),
        )
    except client.exceptions.ApiException as exc:
        if exc.status == 409:
            pass  # Workload already exists (previous attempt interrupted)
        else:
            patch.status["phase"] = "Failed"
            patch.status["message"] = f"Failed to create Workload: {exc.reason}"
            logger.error("Job %s: Workload creation failed: %s", name, exc.reason)
            return

    patch.status["phase"] = "Pending"
    patch.status["message"] = "Workload created, waiting for Kueue admission"
    patch.status["conditions"] = [
        {
            "type": COND_WORKLOAD_ADMITTED,
            "status": "False",
            "reason": "Pending",
            "message": "Workload created, waiting for Kueue admission",
            "lastTransitionTime": _utcnow(),
        }
    ]
    logger.info("Job %s: created Workload, phase=Pending", name)


# ---------------------------------------------------------------------------
# TIMER — periodic reconciliation of the state machine
# ---------------------------------------------------------------------------


@kopf.timer(
    "fournos.dev",
    "v1",
    "fournosjobs",
    interval=5.0,
    when=lambda status, **_: status.get("phase") in ("Pending", "Admitted", "Running"),
)
def reconcile(spec, name, namespace, status, patch, **_):
    phase = status.get("phase", "")

    if phase == "Pending":
        _reconcile_pending(name, status, patch)
    elif phase == "Admitted":
        _reconcile_admitted(spec, name, namespace, status, patch)
    elif phase == "Running":
        _reconcile_running(name, status, patch)


def _reconcile_pending(name, status, patch):
    wl = _kueue.get_workload_or_none(name)
    if wl is None:
        logger.info("Job %s: Workload not yet visible", name)
        return

    conditions = list(status.get("conditions") or [])

    if KueueClient.is_admitted(wl):
        cluster = KueueClient.get_assigned_flavor(wl)
        if not cluster:
            patch.status["phase"] = "Failed"
            patch.status["message"] = "Workload admitted but no flavor assigned"
            _set_condition(
                patch,
                conditions,
                COND_WORKLOAD_ADMITTED,
                "False",
                "NoFlavorAssigned",
                "Workload was admitted but no ResourceFlavor was assigned",
            )
            _kueue.delete_workload(name)
            logger.error("Job %s: admitted without assigned flavor", name)
            return
        patch.status["phase"] = "Admitted"
        patch.status["cluster"] = cluster
        patch.status["message"] = f"Workload admitted, assigned to cluster {cluster}"
        _set_condition(
            patch,
            conditions,
            COND_WORKLOAD_ADMITTED,
            "True",
            "Admitted",
            f"Assigned to cluster {cluster}",
        )
        logger.info("Job %s: Workload admitted, cluster=%s", name, cluster)
    else:
        wl_reason, wl_message = KueueClient.get_pending_message(wl)
        new_msg = (
            f"Waiting for admission: {wl_message}"
            if wl_message
            else "Waiting for Kueue admission"
        )
        if status.get("message") != new_msg:
            patch.status["message"] = new_msg
            _set_condition(
                patch,
                conditions,
                COND_WORKLOAD_ADMITTED,
                "False",
                wl_reason or "Pending",
                wl_message or "Workload is queued for Kueue admission",
            )
        logger.info("Job %s: Workload pending admission", name)


def _reconcile_admitted(spec, name, namespace, status, patch):
    pr = _tekton.get_pipeline_run_or_none(name)
    conditions = list(status.get("conditions") or [])

    if pr is None:
        cluster = status.get("cluster", "")
        secret = _registry.resolve_kubeconfig_secret(cluster)
        hardware = spec.get("hardware")
        gpu_count = hardware.get("gpuCount", 0) if hardware else 0

        display_name = spec.get("displayName") or name

        try:
            _tekton.create_pipeline_run(
                name=name,
                display_name=display_name,
                pipeline=spec.get("pipeline", "fournos-full"),
                forge_project=spec["forge"]["project"],
                forge_args=spec["forge"]["args"],
                forge_config_overrides=spec["forge"].get("configOverrides", {}),
                env=spec.get("env", {}),
                kubeconfig_secret=secret,
                gpu_count=gpu_count,
                secret_refs=spec.get("secretRefs", []),
                cluster=cluster,
            )
            logger.info(
                "Job %s: created PipelineRun for target cluster %s", name, cluster
            )
        except client.exceptions.ApiException as exc:
            if exc.status != 409:
                patch.status["phase"] = "Failed"
                patch.status["message"] = f"Failed to create PipelineRun: {exc.reason}"
                _set_condition(
                    patch,
                    conditions,
                    COND_PIPELINE_RUN_READY,
                    "False",
                    "CreateFailed",
                    f"Failed to create PipelineRun: {exc.reason}",
                )
                _kueue.delete_workload(name)
                logger.error(
                    "Job %s: PipelineRun creation failed (HTTP %s): %s",
                    name,
                    exc.status,
                    exc.reason,
                )
                return
            logger.info("Job %s: PipelineRun already exists (409), proceeding", name)

    patch.status["phase"] = "Running"
    patch.status["pipelineRun"] = f"fournos-{name}"
    patch.status["message"] = "PipelineRun created, waiting for execution"
    _set_condition(
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
            f"{base}/#/namespaces/{namespace}/pipelineruns/fournos-{name}"
        )


def _reconcile_running(name, status, patch):
    pr = _tekton.get_pipeline_run_or_none(name)
    conditions = list(status.get("conditions") or [])

    if pr is None:
        patch.status["phase"] = "Failed"
        patch.status["message"] = "PipelineRun not found"
        _set_condition(
            patch,
            conditions,
            COND_PIPELINE_RUN_READY,
            "False",
            "NotFound",
            f"PipelineRun fournos-{name} not found",
        )
        _kueue.delete_workload(name)
        logger.error("Job %s: PipelineRun fournos-%s not found", name, name)
        return

    pr_status, pr_message = TektonClient.extract_status(pr)
    logger.info(
        "Job %s: PipelineRun status=%s, message=%s", name, pr_status, pr_message
    )
    if pr_status == "succeeded":
        patch.status["phase"] = "Succeeded"
        patch.status["message"] = "Pipeline completed successfully"
        _set_condition(
            patch,
            conditions,
            COND_PIPELINE_RUN_READY,
            "True",
            "Succeeded",
            pr_message or "Pipeline completed successfully",
        )
        _kueue.delete_workload(name)
        logger.info("Job %s: succeeded", name)
    elif pr_status == "failed":
        patch.status["phase"] = "Failed"
        patch.status["message"] = pr_message or "PipelineRun failed"
        _set_condition(
            patch,
            conditions,
            COND_PIPELINE_RUN_READY,
            "False",
            "Failed",
            pr_message or "PipelineRun failed",
        )
        _kueue.delete_workload(name)
        logger.warning("Job %s: PipelineRun failed: %s", name, pr_message)
    else:
        new_msg = (
            f"Pipeline running: {pr_message}" if pr_message else "Pipeline running"
        )
        if status.get("message") != new_msg:
            patch.status["message"] = new_msg
            _set_condition(
                patch,
                conditions,
                COND_PIPELINE_RUN_READY,
                "Unknown",
                "Running",
                pr_message or "PipelineRun is executing",
            )


# ---------------------------------------------------------------------------
# RESOURCE GC — delete stale Workloads/PipelineRuns whose FournosJob is gone
# ---------------------------------------------------------------------------


def _gc_loop():
    interval = settings.gc_interval_sec
    while True:
        time.sleep(interval)
        try:
            _gc_stale_resources()
        except Exception:
            logger.exception("Resource GC failed")


def _gc_stale_resources():
    custom = client.CustomObjectsApi()
    jobs = custom.list_namespaced_custom_object(
        "fournos.dev",
        "v1",
        settings.namespace,
        "fournosjobs",
    )
    job_names = {j["metadata"]["name"] for j in jobs.get("items", [])}

    for wl in _kueue.list_workloads():
        job_name = wl["metadata"].get("labels", {}).get(LABEL_JOB_NAME, "")
        if job_name and job_name not in job_names:
            logger.info("GC: deleting stale Workload for job %s", job_name)
            _kueue.delete_workload(job_name)

    for pr in _tekton.list_pipeline_runs():
        job_name = pr["metadata"].get("labels", {}).get(LABEL_JOB_NAME, "")
        if job_name and job_name not in job_names:
            logger.info("GC: deleting stale PipelineRun for job %s", job_name)
            _tekton.delete_pipeline_run(job_name)


# ---------------------------------------------------------------------------
# DELETE — clean up owned resources
# ---------------------------------------------------------------------------


@kopf.on.delete("fournos.dev", "v1", "fournosjobs")
def on_delete(name, namespace, **_):
    _kueue.delete_workload(name)
    _tekton.delete_pipeline_run(name)
    logger.info("Job %s: cleaned up Workload and PipelineRun", name)
