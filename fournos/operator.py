"""Fournos Kubernetes operator — kopf wiring layer.

Registers kopf handlers (startup, create/resume, timer) and delegates all
business logic to ``handlers`` and ``core.locking``.  Shared clients live
in ``state.ctx``.
"""

from __future__ import annotations

import logging
import threading
import time

import kopf
from kubernetes import client, config

from fournos.core.clusters import ClusterRegistry
from fournos.core.constants import LABEL_JOB_NAME, Phase
from fournos.core.kueue import KueueClient
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

    logger.info("Operating in namespace %s", settings.namespace)

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
    when=lambda status, **_: status.get("phase")
    in (Phase.BLOCKED, Phase.PENDING, Phase.ADMITTED, Phase.RUNNING),
)
def reconcile(spec, name, namespace, status, patch, body, **_):
    phase = status.get("phase", "")

    if phase == Phase.BLOCKED:
        handlers.reconcile_blocked(spec, name, status, patch, body)
    elif phase == Phase.PENDING:
        handlers.reconcile_pending(spec, name, status, patch, body)
    elif phase == Phase.ADMITTED:
        handlers.reconcile_admitted(spec, name, namespace, status, patch, body)
    elif phase == Phase.RUNNING:
        handlers.reconcile_running(name, status, patch)


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
