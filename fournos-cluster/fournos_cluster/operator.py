from __future__ import annotations

import logging

import kopf
from kubernetes import client, config

from fournos_cluster.constants import (
    CRD_GROUP,
    CRD_VERSION,
    FOURNOS_CLUSTER_PLURAL,
    LABEL_CLUSTER_KUBECONFIG,
)
from fournos_cluster.core.gpu_discovery import GPUDiscoveryClient
from fournos_cluster.core.kueue import KueueClient
from fournos_cluster.handlers import cluster, secret
from fournos_cluster.settings import settings
from fournos_cluster.state import ctx

logger = logging.getLogger(__name__)


@kopf.on.startup()
def startup(**_kwargs):
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(level=log_level, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()

    core = client.CoreV1Api()
    custom = client.CustomObjectsApi()

    ctx.kueue = KueueClient(custom)
    ctx.gpu_discovery = GPUDiscoveryClient(core)

    logger.info("FournosCluster controller started (namespace=%s)", settings.namespace)


# ---------------------------------------------------------------------------
# Secret watches — auto-discovery
# ---------------------------------------------------------------------------


@kopf.on.create("", "v1", "secrets", labels={LABEL_CLUSTER_KUBECONFIG: "true"})
@kopf.on.update("", "v1", "secrets", labels={LABEL_CLUSTER_KUBECONFIG: "true"})
def on_kubeconfig_secret(body, name, namespace, **kwargs):
    secret.on_kubeconfig_secret(body=body, name=name, namespace=namespace, **kwargs)


# ---------------------------------------------------------------------------
# FournosCluster watches
# ---------------------------------------------------------------------------


@kopf.on.create(CRD_GROUP, CRD_VERSION, FOURNOS_CLUSTER_PLURAL)
@kopf.on.resume(CRD_GROUP, CRD_VERSION, FOURNOS_CLUSTER_PLURAL)
def on_cluster_create(spec, name, namespace, status, patch, body, **kwargs):
    cluster.on_cluster_create(
        spec=spec, name=name, namespace=namespace, status=status, patch=patch, body=body, **kwargs
    )


@kopf.on.field(CRD_GROUP, CRD_VERSION, FOURNOS_CLUSTER_PLURAL, field="spec.owner")
def on_owner_change(spec, name, namespace, status, patch, body, old, new, **kwargs):
    cluster.on_owner_change(
        spec=spec, name=name, namespace=namespace, status=status,
        patch=patch, body=body, old=old, new=new, **kwargs
    )


@kopf.on.field(CRD_GROUP, CRD_VERSION, FOURNOS_CLUSTER_PLURAL, field="spec.hardware")
def on_hardware_change(spec, name, old, new, patch, **kwargs):
    cluster.on_hardware_change(spec=spec, name=name, old=old, new=new, patch=patch, **kwargs)


@kopf.timer(
    CRD_GROUP,
    CRD_VERSION,
    FOURNOS_CLUSTER_PLURAL,
    interval=settings.reconcile_interval_sec,
)
def reconcile_cluster(spec, name, namespace, status, patch, body, **kwargs):
    cluster.reconcile(
        spec=spec, name=name, namespace=namespace, status=status, patch=patch, body=body, **kwargs
    )
