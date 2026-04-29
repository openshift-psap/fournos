"""Fournos Kubernetes operator — kopf wiring layer.

Registers kopf handlers (startup, create/resume, timer) and delegates all
business logic to ``handlers``.  Shared clients live in ``state.ctx``.
"""

from __future__ import annotations

import logging
import threading
import time

import kopf
from kubernetes import client, config

from fournos.core.clusters import ClusterRegistry
from fournos.core.constants import LABEL_JOB_NAME, Phase
from fournos.core.gpu_discovery import GPUDiscoveryClient
from fournos.core.kueue import KueueClient
from fournos.core.psapcluster import PSAPClusterManager
from fournos.core.resolve import ResolveClient
from fournos.core.tekton import TektonClient
from fournos import __version__, handlers
from fournos.settings import settings
from fournos.state import ctx

logger = logging.getLogger(__name__)

BANNER = rf"""
 _____                              
|  ___|__  _   _ _ __ _ __   ___  ___ 
| |_ / _ \| | | | '__| '_ \ / _ \/ __|
|  _| (_) | |_| | |  | | | | (_) \__ \
|_|  \___/ \__,_|_|  |_| |_|\___/|___/
                                v{__version__}
"""


# ---------------------------------------------------------------------------
# STARTUP — initialise clients and background GC
# ---------------------------------------------------------------------------


@kopf.on.startup()
def startup(**_):
    logging.getLogger("fournos").setLevel(settings.log_level.upper())
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

    for line in BANNER.strip().splitlines():
        logger.info(line)

    try:
        config.load_incluster_config()
        logger.info("Loaded in-cluster Kubernetes config")
    except config.ConfigException:
        config.load_kube_config()
        logger.info("Loaded local kubeconfig")

    custom_objects = client.CustomObjectsApi()
    ctx.kueue = KueueClient(custom_objects)
    ctx.tekton = TektonClient(custom_objects)
    ctx.registry = ClusterRegistry(client.CoreV1Api())
    ctx.resolve = ResolveClient(client.BatchV1Api())
    ctx.gpu_discovery = GPUDiscoveryClient(client.CoreV1Api())
    ctx.psapcluster = PSAPClusterManager(custom_objects)

    logger.info("Operating in namespace %s", settings.namespace)
    logger.info("Secrets namespace: %s", settings.secrets_namespace)

    gc_thread = threading.Thread(target=_gc_loop, daemon=True)
    gc_thread.start()
    logger.info("Resource GC started (interval=%ss)", settings.gc_interval_sec)


# ---------------------------------------------------------------------------
# CREATE / RESUME
# ---------------------------------------------------------------------------


@kopf.on.create("fournos.dev", "v1", "fournosjobs")
@kopf.on.resume("fournos.dev", "v1", "fournosjobs")
def on_create(spec, name, namespace, status, patch, body, **_):
    handlers.on_create(spec, name, namespace, status, patch, body)


# ---------------------------------------------------------------------------
# TIMER — periodic reconciliation of the state machine
# ---------------------------------------------------------------------------


@kopf.timer(
    "fournos.dev",
    "v1",
    "fournosjobs",
    interval=5.0,
    when=lambda status, **_: (
        status.get("phase")
        in (
            Phase.RESOLVING,
            Phase.PENDING,
            Phase.ADMITTED,
            Phase.RUNNING,
            Phase.STOPPING,
        )
    ),
)
def reconcile(spec, name, namespace, status, patch, body, **_):
    phase = status.get("phase", "")

    if phase == Phase.STOPPING:
        handlers.reconcile_stopping(name, status, patch)
        return

    shutdown = spec.get("shutdown")
    if shutdown is not None:
        handlers.handle_shutdown(name, status, patch, shutdown)
        return

    if phase == Phase.RESOLVING:
        handlers.reconcile_resolving(spec, name, status, patch, body)
    elif phase == Phase.PENDING:
        handlers.reconcile_pending(spec, name, status, patch, body)
    elif phase == Phase.ADMITTED:
        handlers.reconcile_admitted(spec, name, namespace, status, patch, body)
    elif phase == Phase.RUNNING:
        handlers.reconcile_running(name, status, patch)


# ---------------------------------------------------------------------------
# PSAPCLUSTER — cluster inventory, GPU discovery, locking
# ---------------------------------------------------------------------------


@kopf.on.create("fournos.dev", "v1", "psapclusters")
@kopf.on.resume("fournos.dev", "v1", "psapclusters")
def on_psapcluster_create(spec, name, namespace, status, patch, body, **_):
    handlers.on_psapcluster_create(spec, name, namespace, status, patch, body)


@kopf.on.update("fournos.dev", "v1", "psapclusters", field="spec.owner")
def on_psapcluster_owner_change(spec, name, namespace, status, patch, body, old, new, **_):
    handlers.on_psapcluster_owner_change(spec, name, namespace, status, patch, body, old, new)


@kopf.timer(
    "fournos.dev",
    "v1",
    "psapclusters",
    interval=settings.psapcluster_timer_interval_sec,
)
def reconcile_psapcluster(spec, name, namespace, status, patch, body, **_):
    handlers.reconcile_psapcluster(spec, name, namespace, status, patch, body)


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

    for wl in ctx.kueue.list_workloads():
        job_name = wl["metadata"].get("labels", {}).get(LABEL_JOB_NAME, "")
        if job_name and job_name not in job_names:
            logger.info("GC: deleting stale Workload for job %s", job_name)
            ctx.kueue.delete_workload(job_name)

    for pr in ctx.tekton.list_pipeline_runs():
        job_name = pr["metadata"].get("labels", {}).get(LABEL_JOB_NAME, "")
        if job_name and job_name not in job_names:
            logger.info("GC: deleting stale PipelineRun for job %s", job_name)
            ctx.tekton.delete_pipeline_run(job_name)
