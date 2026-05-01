"""Auto-discovery of target clusters from kubeconfig secrets."""

from __future__ import annotations

import logging

from kubernetes import client

from fournos.core.clusters import extract_cluster_name, list_kubeconfig_secrets
from fournos.core.constants import LABEL_AUTO_DISCOVERED, LABEL_MANAGED_BY
from fournos.core.kueue import KueueClient
from fournos.settings import settings

logger = logging.getLogger(__name__)

CRD_GROUP = "fournos.dev"
CRD_VERSION = "v1"
PSAPCLUSTER_PLURAL = "psapclusters"


class ClusterDiscovery:
    """Scans for kubeconfig secrets and auto-creates cluster resources."""

    def __init__(
        self,
        k8s_core: client.CoreV1Api,
        k8s_custom: client.CustomObjectsApi,
        kueue: KueueClient,
    ) -> None:
        self._k8s_core = k8s_core
        self._k8s_custom = k8s_custom
        self._kueue = kueue

    def scan(self) -> list[str]:
        """Scan for new clusters and onboard them.

        Returns the list of newly discovered cluster names.
        """
        secrets = list_kubeconfig_secrets(self._k8s_core)
        if not secrets:
            return []

        existing_flavors = self._kueue.list_flavors()
        existing_clusters = self._list_existing_psapclusters()

        discovered: list[str] = []
        for secret_name in secrets:
            cluster_name = extract_cluster_name(secret_name)
            if cluster_name is None:
                continue

            if cluster_name in existing_clusters:
                continue

            logger.info("Discovered new cluster %s from secret %s", cluster_name, secret_name)

            if cluster_name not in existing_flavors:
                self._kueue.create_flavor(cluster_name)

            self._kueue.add_flavor_to_cluster_queue(cluster_name)
            self._create_psapcluster(cluster_name, secret_name)
            discovered.append(cluster_name)

        return discovered

    def _list_existing_psapclusters(self) -> set[str]:
        result = self._k8s_custom.list_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=settings.namespace,
            plural=PSAPCLUSTER_PLURAL,
        )
        return {item["metadata"]["name"] for item in result.get("items", [])}

    def _create_psapcluster(self, cluster_name: str, secret_name: str) -> None:
        body = {
            "apiVersion": f"{CRD_GROUP}/{CRD_VERSION}",
            "kind": "PSAPCluster",
            "metadata": {
                "name": cluster_name,
                "namespace": settings.namespace,
                "labels": {
                    LABEL_MANAGED_BY: "fournos",
                    LABEL_AUTO_DISCOVERED: "true",
                },
            },
            "spec": {
                "kubeconfigSecret": secret_name,
            },
        }
        try:
            self._k8s_custom.create_namespaced_custom_object(
                group=CRD_GROUP,
                version=CRD_VERSION,
                namespace=settings.namespace,
                plural=PSAPCLUSTER_PLURAL,
                body=body,
            )
            logger.info("Created PSAPCluster %s", cluster_name)
        except client.exceptions.ApiException as exc:
            if exc.status == 409:
                logger.debug("PSAPCluster %s already exists", cluster_name)
            else:
                raise
