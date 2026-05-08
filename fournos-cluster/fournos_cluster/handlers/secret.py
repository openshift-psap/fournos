from __future__ import annotations

import logging
import re

from kubernetes import client

from fournos_cluster.constants import (
    CRD_GROUP,
    CRD_VERSION,
    FOURNOS_CLUSTER_PLURAL,
    LABEL_AUTO_DISCOVERED,
    LABEL_MANAGED_BY,
)
from fournos_cluster.settings import settings

logger = logging.getLogger(__name__)

_CLUSTER_NAME_RE: re.Pattern[str] | None = None


def _build_cluster_name_regex(pattern: str) -> re.Pattern[str]:
    escaped = re.escape(pattern)
    regex = escaped.replace(r"\{cluster\}", r"(?P<cluster>.+)")
    return re.compile(f"^{regex}$")


def _get_cluster_name_re() -> re.Pattern[str]:
    global _CLUSTER_NAME_RE
    if _CLUSTER_NAME_RE is None:
        _CLUSTER_NAME_RE = _build_cluster_name_regex(settings.kubeconfig_secret_pattern)
    return _CLUSTER_NAME_RE


def extract_cluster_name(secret_name: str) -> str | None:
    m = _get_cluster_name_re().match(secret_name)
    return m.group("cluster") if m else None


def on_kubeconfig_secret(body, name, namespace, **kwargs):
    cluster_name = extract_cluster_name(name)
    if cluster_name is None:
        logger.warning("Secret %s matched label but not name pattern, skipping", name)
        return

    custom = client.CustomObjectsApi()

    try:
        custom.get_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=settings.namespace,
            plural=FOURNOS_CLUSTER_PLURAL,
            name=cluster_name,
        )
        logger.debug("FournosCluster %s already exists", cluster_name)
        return
    except client.exceptions.ApiException as exc:
        if exc.status != 404:
            raise

    cr_body = {
        "apiVersion": f"{CRD_GROUP}/{CRD_VERSION}",
        "kind": "FournosCluster",
        "metadata": {
            "name": cluster_name,
            "namespace": settings.namespace,
            "labels": {
                LABEL_AUTO_DISCOVERED: "true",
                LABEL_MANAGED_BY: "fournos-cluster",
            },
        },
        "spec": {
            "kubeconfigSecret": name,
        },
    }

    annotations = (body.get("metadata") or {}).get("annotations") or {}
    owner = annotations.get("fournos.dev/owner")
    if owner:
        cr_body["spec"]["owner"] = owner

    try:
        custom.create_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=settings.namespace,
            plural=FOURNOS_CLUSTER_PLURAL,
            body=cr_body,
        )
        logger.info("Auto-discovered cluster %s from secret %s", cluster_name, name)
    except client.exceptions.ApiException as exc:
        if exc.status == 409:
            logger.debug("FournosCluster %s already exists (race)", cluster_name)
        else:
            raise
