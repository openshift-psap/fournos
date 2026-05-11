from __future__ import annotations

import logging

from kubernetes import client

from fournos.core.constants import (
    CLUSTER_SLOT_RESOURCE,
    LABEL_JOB_NAME,
    LABEL_MANAGED_BY,
    MAX_CLUSTER_SLOTS,
)
from fournos.settings import settings

logger = logging.getLogger(__name__)

KUEUE_GROUP = "kueue.x-k8s.io"
KUEUE_VERSION = "v1beta2"
KUEUE_WORKLOAD_PLURAL = "workloads"
KUEUE_RESOURCE_FLAVOR_PLURAL = "resourceflavors"
KUEUE_CLUSTER_QUEUE_PLURAL = "clusterqueues"


class KueueClient:
    def __init__(self, k8s_client: client.CustomObjectsApi) -> None:
        self._k8s = k8s_client

    @staticmethod
    def _gpu_resource_name(gpu_type: str) -> str:
        return f"{settings.gpu_resource_prefix}{gpu_type.lower()}"

    def create_workload(
        self,
        *,
        name: str,
        gpu_type: str | None = None,
        gpu_count: int = 0,
        cluster: str | None = None,
        exclusive: bool = True,
        priority: str | None = None,
        owner_ref: dict | None = None,
    ) -> dict:
        resource_requests: dict[str, str] = {}
        if gpu_type and gpu_count:
            gpu_resource = self._gpu_resource_name(gpu_type)
            resource_requests[gpu_resource] = str(gpu_count)

        slots = MAX_CLUSTER_SLOTS if exclusive else 1
        resource_requests[CLUSTER_SLOT_RESOURCE] = str(slots)

        pod_spec: dict = {
            "containers": [
                {
                    "name": "placeholder",
                    "image": "registry.k8s.io/pause:3.9",
                    "resources": {"requests": resource_requests},
                }
            ],
            "restartPolicy": "Never",
        }

        if cluster:
            pod_spec["nodeSelector"] = {"fournos.dev/cluster": cluster}

        metadata: dict = {
            "name": name,
            "namespace": settings.workload_namespace,
            "labels": {
                "kueue.x-k8s.io/queue-name": settings.kueue_local_queue_name,
                LABEL_MANAGED_BY: "fournos",
                LABEL_JOB_NAME: name,
            },
        }
        if owner_ref:
            metadata["ownerReferences"] = [owner_ref]

        body: dict = {
            "apiVersion": f"{KUEUE_GROUP}/{KUEUE_VERSION}",
            "kind": "Workload",
            "metadata": metadata,
            "spec": {
                "queueName": settings.kueue_local_queue_name,
                "podSets": [
                    {
                        "name": "launcher",
                        "count": 1,
                        "template": {"spec": pod_spec},
                    }
                ],
            },
        }

        if priority:
            body["spec"]["priorityClassName"] = priority

        result = self._k8s.create_namespaced_custom_object(
            group=KUEUE_GROUP,
            version=KUEUE_VERSION,
            namespace=settings.workload_namespace,
            plural=KUEUE_WORKLOAD_PLURAL,
            body=body,
        )
        logger.info("Created Kueue Workload %s", name)
        return result

    def get_workload(self, name: str) -> dict:
        return self._k8s.get_namespaced_custom_object(
            group=KUEUE_GROUP,
            version=KUEUE_VERSION,
            namespace=settings.workload_namespace,
            plural=KUEUE_WORKLOAD_PLURAL,
            name=name,
        )

    def get_workload_or_none(self, name: str) -> dict | None:
        try:
            return self.get_workload(name)
        except client.exceptions.ApiException as exc:
            if exc.status == 404:
                return None
            raise

    def list_workloads(self) -> list[dict]:
        result = self._k8s.list_namespaced_custom_object(
            group=KUEUE_GROUP,
            version=KUEUE_VERSION,
            namespace=settings.workload_namespace,
            plural=KUEUE_WORKLOAD_PLURAL,
            label_selector=f"{LABEL_MANAGED_BY}=fournos",
        )
        return result.get("items", [])

    def list_flavors(self) -> set[str]:
        """Return the set of ResourceFlavor names known to Kueue."""
        result = self._k8s.list_cluster_custom_object(
            group=KUEUE_GROUP,
            version=KUEUE_VERSION,
            plural=KUEUE_RESOURCE_FLAVOR_PLURAL,
        )
        return {item["metadata"]["name"] for item in result.get("items", [])}

    def list_gpu_types(self) -> set[str]:
        """Return the set of GPU type short names that have quota in any ClusterQueue."""
        result = self._k8s.list_cluster_custom_object(
            group=KUEUE_GROUP,
            version=KUEUE_VERSION,
            plural=KUEUE_CLUSTER_QUEUE_PLURAL,
        )
        prefix = settings.gpu_resource_prefix
        gpu_types: set[str] = set()
        for cq in result.get("items", []):
            for rg in cq.get("spec", {}).get("resourceGroups", []):
                for resource in rg.get("coveredResources", []):
                    if resource.startswith(prefix):
                        gpu_types.add(resource[len(prefix) :])
        return gpu_types

    @staticmethod
    def is_admitted(workload: dict) -> bool:
        conditions = workload.get("status", {}).get("conditions", [])
        return any(
            c.get("type") == "Admitted" and c.get("status") == "True"
            for c in conditions
        )

    @staticmethod
    def get_pending_message(workload: dict) -> tuple[str, str]:
        """Return (reason, message) from the most relevant Workload condition."""
        conditions = workload.get("status", {}).get("conditions", [])
        for c in reversed(conditions):
            if c.get("message"):
                return c.get("reason", ""), c.get("message", "")
        return "", ""

    @staticmethod
    def get_assigned_flavor(workload: dict) -> str | None:
        """Extract the assigned ResourceFlavor (= cluster name) from an admitted Workload."""
        admission = workload.get("status", {}).get("admission", {})
        pod_set_assignments = admission.get("podSetAssignments", [])
        if not pod_set_assignments:
            return None
        flavors = pod_set_assignments[0].get("flavors", {})
        if flavors:
            return next(iter(flavors.values()))
        return None

    def create_flavor(self, flavor_name: str) -> dict | None:
        """Create a ResourceFlavor with a nodeLabel pointing to the cluster.

        Returns the created object, or ``None`` if it already exists (409).
        """
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
        """Add a flavor entry to the global ClusterQueue.

        Read-modify-write: GET the CQ, check if flavor already present,
        append a new flavor entry with cluster-slot quota and zero GPU
        quotas for all existing coveredResources, PATCH back.

        Returns the patched CQ, or ``None`` if already present or CQ not found.
        """
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
        """Update GPU quotas for a specific flavor in the global ClusterQueue.

        Read-modify-write: GET the CQ, find the matching flavor, update its
        GPU resources, add new GPU types to coveredResources if needed, and
        PATCH back.  Preserves cluster-slot quota in every flavor.
        """
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
            (r for r in target_flavor.get("resources", [])
             if r["name"] == CLUSTER_SLOT_RESOURCE),
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

    def delete_workload(self, name: str) -> None:
        """Delete the virtual Workload to release Kueue quota."""
        try:
            self._k8s.delete_namespaced_custom_object(
                group=KUEUE_GROUP,
                version=KUEUE_VERSION,
                namespace=settings.workload_namespace,
                plural=KUEUE_WORKLOAD_PLURAL,
                name=name,
            )
            logger.info("Deleted Kueue Workload %s", name)
        except client.exceptions.ApiException as exc:
            if exc.status != 404:
                raise
