from __future__ import annotations

import logging

from kubernetes import client

from fournos_cluster.constants import CLUSTER_SLOT_RESOURCE, MAX_CLUSTER_SLOTS
from fournos_cluster.settings import settings

logger = logging.getLogger(__name__)

KUEUE_GROUP = "kueue.x-k8s.io"
KUEUE_VERSION = "v1beta2"
KUEUE_RESOURCE_FLAVOR_PLURAL = "resourceflavors"
KUEUE_CLUSTER_QUEUE_PLURAL = "clusterqueues"


class KueueClient:
    def __init__(self, k8s_client: client.CustomObjectsApi) -> None:
        self._k8s = k8s_client

    @staticmethod
    def _gpu_resource_name(gpu_type: str) -> str:
        return f"{settings.gpu_resource_prefix}{gpu_type.lower()}"

    def list_flavors(self) -> set[str]:
        result = self._k8s.list_cluster_custom_object(
            group=KUEUE_GROUP,
            version=KUEUE_VERSION,
            plural=KUEUE_RESOURCE_FLAVOR_PLURAL,
        )
        return {item["metadata"]["name"] for item in result.get("items", [])}

    def create_flavor(self, flavor_name: str) -> dict | None:
        body = {
            "apiVersion": f"{KUEUE_GROUP}/{KUEUE_VERSION}",
            "kind": "ResourceFlavor",
            "metadata": {"name": flavor_name},
            "spec": {
                "nodeLabels": {"fournos.dev/cluster": flavor_name},
            },
        }
        try:
            result = self._k8s.create_cluster_custom_object(
                group=KUEUE_GROUP,
                version=KUEUE_VERSION,
                plural=KUEUE_RESOURCE_FLAVOR_PLURAL,
                body=body,
            )
            logger.info("Created ResourceFlavor %s", flavor_name)
            return result
        except client.exceptions.ApiException as exc:
            if exc.status == 409:
                logger.debug("ResourceFlavor %s already exists", flavor_name)
                return None
            raise

    def add_flavor_to_cluster_queue(self, flavor_name: str) -> dict | None:
        cq_name = settings.kueue_cluster_queue_name
        try:
            cq = self._k8s.get_cluster_custom_object(
                group=KUEUE_GROUP,
                version=KUEUE_VERSION,
                plural=KUEUE_CLUSTER_QUEUE_PLURAL,
                name=cq_name,
            )
        except client.exceptions.ApiException as exc:
            if exc.status == 404:
                logger.warning("ClusterQueue %s not found", cq_name)
                return None
            raise

        resource_groups = cq.get("spec", {}).get("resourceGroups", [])
        if not resource_groups:
            logger.warning("ClusterQueue %s has no resourceGroups", cq_name)
            return None

        rg = resource_groups[0]
        flavors = rg.get("flavors", [])

        for f in flavors:
            if f["name"] == flavor_name:
                logger.debug("Flavor %s already in ClusterQueue %s", flavor_name, cq_name)
                return None

        covered = rg.get("coveredResources", [])
        resources: list[dict] = []
        for resource_name in covered:
            if resource_name == CLUSTER_SLOT_RESOURCE:
                resources.append({"name": CLUSTER_SLOT_RESOURCE, "nominalQuota": MAX_CLUSTER_SLOTS})
            else:
                resources.append({"name": resource_name, "nominalQuota": 0})

        if not any(r["name"] == CLUSTER_SLOT_RESOURCE for r in resources):
            resources.append({"name": CLUSTER_SLOT_RESOURCE, "nominalQuota": MAX_CLUSTER_SLOTS})

        flavors.append({"name": flavor_name, "resources": resources})
        rg["flavors"] = flavors

        result = self._k8s.patch_cluster_custom_object(
            group=KUEUE_GROUP,
            version=KUEUE_VERSION,
            plural=KUEUE_CLUSTER_QUEUE_PLURAL,
            name=cq_name,
            body={"spec": {"resourceGroups": resource_groups}},
        )
        logger.info("Added flavor %s to ClusterQueue %s", flavor_name, cq_name)
        return result

    def update_flavor_quotas(
        self,
        flavor_name: str,
        gpu_resources: list[tuple[str, int]],
    ) -> dict | None:
        cq_name = settings.kueue_cluster_queue_name
        try:
            cq = self._k8s.get_cluster_custom_object(
                group=KUEUE_GROUP,
                version=KUEUE_VERSION,
                plural=KUEUE_CLUSTER_QUEUE_PLURAL,
                name=cq_name,
            )
        except client.exceptions.ApiException as exc:
            if exc.status == 404:
                logger.warning("ClusterQueue %s not found, cannot update quotas", cq_name)
                return None
            raise

        resource_groups = cq.get("spec", {}).get("resourceGroups", [])
        if not resource_groups:
            logger.warning("ClusterQueue %s has no resourceGroups", cq_name)
            return None

        rg = resource_groups[0]
        covered = set(rg.get("coveredResources", []))
        flavors = rg.get("flavors", [])

        target_flavor = None
        for f in flavors:
            if f["name"] == flavor_name:
                target_flavor = f
                break

        if target_flavor is None:
            logger.warning(
                "Flavor %s not found in ClusterQueue %s, cannot update quotas",
                flavor_name,
                cq_name,
            )
            return None

        new_resources: list[dict] = []
        for short_name, count in gpu_resources:
            resource_name = self._gpu_resource_name(short_name)
            covered.add(resource_name)
            new_resources.append({"name": resource_name, "nominalQuota": count})

        existing_slot = next(
            (
                r
                for r in target_flavor.get("resources", [])
                if r["name"] == CLUSTER_SLOT_RESOURCE
            ),
            None,
        )
        slot_quota = existing_slot["nominalQuota"] if existing_slot else MAX_CLUSTER_SLOTS
        new_resources.append({"name": CLUSTER_SLOT_RESOURCE, "nominalQuota": slot_quota})
        covered.add(CLUSTER_SLOT_RESOURCE)

        rg["coveredResources"] = sorted(covered)
        target_flavor["resources"] = new_resources

        result = self._k8s.patch_cluster_custom_object(
            group=KUEUE_GROUP,
            version=KUEUE_VERSION,
            plural=KUEUE_CLUSTER_QUEUE_PLURAL,
            name=cq_name,
            body={"spec": {"resourceGroups": resource_groups}},
        )
        logger.info(
            "Updated ClusterQueue %s flavor %s quotas: %s",
            cq_name,
            flavor_name,
            gpu_resources,
        )
        return result
