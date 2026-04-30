from __future__ import annotations

import logging

from kubernetes import client

from fournos.core.constants import (
    DEFAULT_CLUSTER_SLOTS,
    LABEL_MANAGED_BY,
    LABEL_PSAPCLUSTER,
    PSAPCLUSTER_COHORT,
    PSAPCLUSTER_CQ_PREFIX,
)
from fournos.core.kueue import KUEUE_GROUP, KUEUE_VERSION
from fournos.settings import settings

logger = logging.getLogger(__name__)

RESOURCE_FLAVOR_PLURAL = "resourceflavors"
CLUSTER_QUEUE_PLURAL = "clusterqueues"
LOCAL_QUEUE_PLURAL = "localqueues"


class PSAPClusterManager:
    def __init__(self, k8s_client: client.CustomObjectsApi) -> None:
        self._k8s = k8s_client

    @staticmethod
    def cluster_queue_name(cluster_name: str) -> str:
        return f"{PSAPCLUSTER_CQ_PREFIX}{cluster_name}"

    def ensure_resource_flavor(self, cluster_name: str) -> dict:
        body = {
            "apiVersion": f"{KUEUE_GROUP}/{KUEUE_VERSION}",
            "kind": "ResourceFlavor",
            "metadata": {
                "name": cluster_name,
                "labels": {
                    LABEL_MANAGED_BY: "fournos",
                    LABEL_PSAPCLUSTER: cluster_name,
                },
            },
            "spec": {
                "nodeLabels": {"fournos.dev/cluster": cluster_name},
            },
        }
        try:
            result = self._k8s.create_cluster_custom_object(
                group=KUEUE_GROUP,
                version=KUEUE_VERSION,
                plural=RESOURCE_FLAVOR_PLURAL,
                body=body,
            )
            logger.info("Created ResourceFlavor %s", cluster_name)
            return result
        except client.exceptions.ApiException as exc:
            if exc.status == 409:
                logger.debug("ResourceFlavor %s already exists", cluster_name)
                return self._k8s.get_cluster_custom_object(
                    group=KUEUE_GROUP,
                    version=KUEUE_VERSION,
                    plural=RESOURCE_FLAVOR_PLURAL,
                    name=cluster_name,
                )
            raise

    def ensure_cluster_queue(
        self,
        cluster_name: str,
        gpu_resources: list[tuple[str, int]] | None = None,
        stop_policy: str = "None",
    ) -> dict:
        resources: list[dict] = []
        covered: list[str] = []

        for short_name, count in (gpu_resources or []):
            resource_name = f"{settings.gpu_resource_prefix}{short_name}"
            covered.append(resource_name)
            resources.append({"name": resource_name, "nominalQuota": count})

        covered.append("fournos/cluster-slot")
        resources.append({"name": "fournos/cluster-slot", "nominalQuota": DEFAULT_CLUSTER_SLOTS})

        cq_name = self.cluster_queue_name(cluster_name)
        body = {
            "apiVersion": f"{KUEUE_GROUP}/{KUEUE_VERSION}",
            "kind": "ClusterQueue",
            "metadata": {
                "name": cq_name,
                "labels": {
                    LABEL_MANAGED_BY: "fournos",
                    LABEL_PSAPCLUSTER: cluster_name,
                },
            },
            "spec": {
                "cohort": PSAPCLUSTER_COHORT,
                "namespaceSelector": {
                    "matchLabels": {"fournos.dev/queue-access": "true"},
                },
                "stopPolicy": stop_policy,
                "resourceGroups": [
                    {
                        "coveredResources": covered,
                        "flavors": [
                            {"name": cluster_name, "resources": resources},
                        ],
                    },
                ],
            },
        }
        try:
            result = self._k8s.create_cluster_custom_object(
                group=KUEUE_GROUP,
                version=KUEUE_VERSION,
                plural=CLUSTER_QUEUE_PLURAL,
                body=body,
            )
            logger.info("Created ClusterQueue %s (stopPolicy=%s)", cq_name, stop_policy)
            return result
        except client.exceptions.ApiException as exc:
            if exc.status == 409:
                logger.debug("ClusterQueue %s already exists", cq_name)
                return self._k8s.get_cluster_custom_object(
                    group=KUEUE_GROUP,
                    version=KUEUE_VERSION,
                    plural=CLUSTER_QUEUE_PLURAL,
                    name=cq_name,
                )
            raise

    def ensure_local_queue(self, cluster_name: str, namespace: str) -> dict:
        lq_name = self.cluster_queue_name(cluster_name)
        body = {
            "apiVersion": f"{KUEUE_GROUP}/{KUEUE_VERSION}",
            "kind": "LocalQueue",
            "metadata": {
                "name": lq_name,
                "namespace": namespace,
                "labels": {
                    LABEL_MANAGED_BY: "fournos",
                    LABEL_PSAPCLUSTER: cluster_name,
                },
            },
            "spec": {
                "clusterQueue": lq_name,
            },
        }
        try:
            result = self._k8s.create_namespaced_custom_object(
                group=KUEUE_GROUP,
                version=KUEUE_VERSION,
                namespace=namespace,
                plural=LOCAL_QUEUE_PLURAL,
                body=body,
            )
            logger.info("Created LocalQueue %s in %s", lq_name, namespace)
            return result
        except client.exceptions.ApiException as exc:
            if exc.status == 409:
                logger.debug("LocalQueue %s already exists in %s", lq_name, namespace)
                return self._k8s.get_namespaced_custom_object(
                    group=KUEUE_GROUP,
                    version=KUEUE_VERSION,
                    namespace=namespace,
                    plural=LOCAL_QUEUE_PLURAL,
                    name=lq_name,
                )
            raise

    def set_cluster_queue_stop_policy(self, cluster_name: str, policy: str) -> dict | None:
        cq_name = self.cluster_queue_name(cluster_name)
        body = {"spec": {"stopPolicy": policy}}
        try:
            result = self._k8s.patch_cluster_custom_object(
                group=KUEUE_GROUP,
                version=KUEUE_VERSION,
                plural=CLUSTER_QUEUE_PLURAL,
                name=cq_name,
                body=body,
            )
        except ApiException as exc:
            if exc.status == 404:
                logger.warning("ClusterQueue %s not found, skipping stopPolicy patch", cq_name)
                return None
            raise
        logger.info("Patched ClusterQueue %s stopPolicy=%s", cq_name, policy)
        return result

    def update_cluster_queue_quotas(
        self,
        cluster_name: str,
        gpu_resources: list[tuple[str, int]],
    ) -> dict:
        resources: list[dict] = []
        covered: list[str] = []

        for short_name, count in gpu_resources:
            resource_name = f"{settings.gpu_resource_prefix}{short_name}"
            covered.append(resource_name)
            resources.append({"name": resource_name, "nominalQuota": count})

        covered.append("fournos/cluster-slot")
        resources.append({"name": "fournos/cluster-slot", "nominalQuota": DEFAULT_CLUSTER_SLOTS})

        cq_name = self.cluster_queue_name(cluster_name)
        body = {
            "spec": {
                "resourceGroups": [
                    {
                        "coveredResources": covered,
                        "flavors": [
                            {"name": cluster_name, "resources": resources},
                        ],
                    },
                ],
            },
        }
        result = self._k8s.patch_cluster_custom_object(
            group=KUEUE_GROUP,
            version=KUEUE_VERSION,
            plural=CLUSTER_QUEUE_PLURAL,
            name=cq_name,
            body=body,
        )
        logger.info("Updated ClusterQueue %s quotas: %s", cq_name, gpu_resources)
        return result

    def get_cluster_queue_or_none(self, cluster_name: str) -> dict | None:
        cq_name = self.cluster_queue_name(cluster_name)
        try:
            return self._k8s.get_cluster_custom_object(
                group=KUEUE_GROUP,
                version=KUEUE_VERSION,
                plural=CLUSTER_QUEUE_PLURAL,
                name=cq_name,
            )
        except client.exceptions.ApiException as exc:
            if exc.status == 404:
                return None
            raise

    def get_resource_flavor_or_none(self, cluster_name: str) -> dict | None:
        try:
            return self._k8s.get_cluster_custom_object(
                group=KUEUE_GROUP,
                version=KUEUE_VERSION,
                plural=RESOURCE_FLAVOR_PLURAL,
                name=cluster_name,
            )
        except client.exceptions.ApiException as exc:
            if exc.status == 404:
                return None
            raise
