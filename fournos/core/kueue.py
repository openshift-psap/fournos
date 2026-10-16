from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

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
KUEUE_LOCAL_QUEUE_PLURAL = "localqueues"
KUEUE_RESOURCE_FLAVOR_PLURAL = "resourceflavors"
KUEUE_CLUSTER_QUEUE_PLURAL = "clusterqueues"
GPU_POOL_LABEL_PREFIX = "fournos.dev/gpu-pool-"


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
        gpu_pool: str | None = None,
        exclusive: bool = True,
        priority: str | None = None,
        owner_ref: dict | None = None,
    ) -> dict:
        if cluster and gpu_pool:
            raise ValueError("cluster and gpu_pool are mutually exclusive")

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
        elif gpu_pool:
            if not gpu_type or not gpu_count:
                raise ValueError(
                    "gpu_pool requires a GPU type and a positive GPU count"
                )
            pod_spec["nodeSelector"] = {
                f"{GPU_POOL_LABEL_PREFIX}{gpu_type.lower()}": gpu_pool
            }

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

    def list_gpu_pools(self) -> dict[str, set[str]]:
        """Return GPU pools with GPU and slot quota in this job's ClusterQueue.

        Pools are encoded as ``fournos.dev/gpu-pool-<gpu-type>`` node labels on
        ResourceFlavors. The LocalQueue determines which ClusterQueue can admit
        Workloads from this namespace.
        """
        local_queue = self._k8s.get_namespaced_custom_object(
            group=KUEUE_GROUP,
            version=KUEUE_VERSION,
            namespace=settings.workload_namespace,
            plural=KUEUE_LOCAL_QUEUE_PLURAL,
            name=settings.kueue_local_queue_name,
        )
        cluster_queue_name = local_queue.get("spec", {}).get("clusterQueue")
        if not cluster_queue_name:
            raise ValueError(
                f"LocalQueue '{settings.kueue_local_queue_name}' has no ClusterQueue"
            )
        queue = self._k8s.get_cluster_custom_object(
            group=KUEUE_GROUP,
            version=KUEUE_VERSION,
            plural=KUEUE_CLUSTER_QUEUE_PLURAL,
            name=cluster_queue_name,
        )
        flavor_gpu_types: dict[str, set[str]] = {}
        prefix = settings.gpu_resource_prefix

        for resource_group in queue.get("spec", {}).get("resourceGroups", []):
            covered_resources = set(resource_group.get("coveredResources", []))
            if CLUSTER_SLOT_RESOURCE not in covered_resources:
                continue

            for flavor in resource_group.get("flavors", []):
                flavor_name = flavor.get("name")
                if not flavor_name:
                    continue
                quotas = {
                    resource.get("name"): resource.get("nominalQuota", 0)
                    for resource in flavor.get("resources", [])
                }
                if not self._positive_quota(quotas.get(CLUSTER_SLOT_RESOURCE)):
                    continue
                for resource_name, quota in quotas.items():
                    if not resource_name or not resource_name.startswith(prefix):
                        continue
                    if resource_name not in covered_resources:
                        continue
                    if self._positive_quota(quota):
                        gpu_type = resource_name[len(prefix) :]
                        flavor_gpu_types.setdefault(flavor_name, set()).add(gpu_type)

        resource_flavors = self._k8s.list_cluster_custom_object(
            group=KUEUE_GROUP,
            version=KUEUE_VERSION,
            plural=KUEUE_RESOURCE_FLAVOR_PLURAL,
        )
        pools: dict[str, set[str]] = {}
        for flavor in resource_flavors.get("items", []):
            flavor_name = flavor.get("metadata", {}).get("name")
            if not flavor_name:
                continue
            labels = flavor.get("spec", {}).get("nodeLabels", {})
            for gpu_type in flavor_gpu_types.get(flavor_name, set()):
                pool = labels.get(f"{GPU_POOL_LABEL_PREFIX}{gpu_type}")
                if isinstance(pool, str) and pool:
                    pools.setdefault(gpu_type, set()).add(pool)

        return pools

    @staticmethod
    def _positive_quota(value: object) -> bool:
        try:
            return Decimal(str(value)) > 0
        except (InvalidOperation, TypeError, ValueError):
            return False

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
