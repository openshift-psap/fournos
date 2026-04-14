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
        exclusive: bool = False,
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
            "namespace": settings.namespace,
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
            namespace=settings.namespace,
            plural=KUEUE_WORKLOAD_PLURAL,
            body=body,
        )
        logger.info("Created Kueue Workload %s", name)
        return result

    def get_workload(self, name: str) -> dict:
        return self._k8s.get_namespaced_custom_object(
            group=KUEUE_GROUP,
            version=KUEUE_VERSION,
            namespace=settings.namespace,
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
            namespace=settings.namespace,
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

    def delete_workload(self, name: str) -> None:
        """Delete the virtual Workload to release Kueue quota."""
        try:
            self._k8s.delete_namespaced_custom_object(
                group=KUEUE_GROUP,
                version=KUEUE_VERSION,
                namespace=settings.namespace,
                plural=KUEUE_WORKLOAD_PLURAL,
                name=name,
            )
            logger.info("Deleted Kueue Workload %s", name)
        except client.exceptions.ApiException as exc:
            if exc.status != 404:
                raise
