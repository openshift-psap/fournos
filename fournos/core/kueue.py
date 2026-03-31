from __future__ import annotations

import asyncio
import logging

from kubernetes import client

from fournos.core.constants import LABEL_JOB_ID, LABEL_MANAGED_BY
from fournos.settings import settings

logger = logging.getLogger(__name__)

KUEUE_GROUP = "kueue.x-k8s.io"
KUEUE_VERSION = "v1beta2"
KUEUE_WORKLOAD_PLURAL = "workloads"
KUEUE_RESOURCE_FLAVOR_PLURAL = "resourceflavors"


class KueueClient:
    def __init__(self, k8s_client: client.CustomObjectsApi) -> None:
        self._k8s = k8s_client

    @staticmethod
    def _gpu_resource_name(gpu_type: str) -> str:
        return f"{settings.gpu_resource_prefix}{gpu_type.lower()}"

    def create_workload(
        self,
        *,
        job_id: str,
        job_name: str,
        gpu_type: str | None = None,
        gpu_count: int = 0,
        cluster: str | None = None,
        priority: str | None = None,
    ) -> dict:
        workload_name = f"fournos-{job_id}"

        resource_requests: dict[str, str] = {"cpu": "1"}
        if gpu_type and gpu_count:
            gpu_resource = self._gpu_resource_name(gpu_type)
            resource_requests[gpu_resource] = str(gpu_count)

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

        body: dict = {
            "apiVersion": f"{KUEUE_GROUP}/{KUEUE_VERSION}",
            "kind": "Workload",
            "metadata": {
                "name": workload_name,
                "namespace": settings.namespace,
                "labels": {
                    "kueue.x-k8s.io/queue-name": settings.kueue_local_queue_name,
                    LABEL_MANAGED_BY: "fournos",
                    LABEL_JOB_ID: job_id,
                },
                "annotations": {
                    "fournos.dev/job-name": job_name,
                },
            },
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
        logger.info("Created Kueue Workload %s for job %s", workload_name, job_id)
        return result

    def get_workload(self, job_id: str) -> dict:
        return self._k8s.get_namespaced_custom_object(
            group=KUEUE_GROUP,
            version=KUEUE_VERSION,
            namespace=settings.namespace,
            plural=KUEUE_WORKLOAD_PLURAL,
            name=f"fournos-{job_id}",
        )

    def get_workload_or_none(self, job_id: str) -> dict | None:
        try:
            return self.get_workload(job_id)
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

    @staticmethod
    def is_admitted(workload: dict) -> bool:
        conditions = workload.get("status", {}).get("conditions", [])
        return any(
            c.get("type") == "Admitted" and c.get("status") == "True"
            for c in conditions
        )

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

    def annotate_workload_error(self, job_id: str, error: str) -> None:
        """Set an error annotation on the Workload so the API can surface it."""
        workload_name = f"fournos-{job_id}"
        patch = {"metadata": {"annotations": {"fournos.dev/error": error}}}
        try:
            self._k8s.patch_namespaced_custom_object(
                group=KUEUE_GROUP,
                version=KUEUE_VERSION,
                namespace=settings.namespace,
                plural=KUEUE_WORKLOAD_PLURAL,
                name=workload_name,
                body=patch,
            )
            logger.info("Annotated Workload %s with error", workload_name)
        except client.exceptions.ApiException as exc:
            if exc.status != 404:
                raise

    def delete_workload(self, job_id: str) -> None:
        """Delete the virtual Workload to release Kueue quota."""
        workload_name = f"fournos-{job_id}"
        try:
            self._k8s.delete_namespaced_custom_object(
                group=KUEUE_GROUP,
                version=KUEUE_VERSION,
                namespace=settings.namespace,
                plural=KUEUE_WORKLOAD_PLURAL,
                name=workload_name,
            )
            logger.info("Deleted Kueue Workload %s", workload_name)
        except client.exceptions.ApiException as exc:
            if exc.status != 404:
                raise

    async def poll_admission(self, job_id: str) -> str | None:
        """Block until the Workload is admitted; return the assigned flavor (cluster).

        Returns ``None`` if the Workload is deleted before admission (e.g. by
        manual cleanup, or test teardown).
        """
        deadline = asyncio.get_event_loop().time() + settings.admission_poll_timeout_sec
        while True:
            workload = await asyncio.to_thread(self.get_workload_or_none, job_id)
            if workload is None:
                logger.info(
                    "Workload fournos-%s disappeared, stopping admission poll",
                    job_id,
                )
                return None
            if self.is_admitted(workload):
                flavor = self.get_assigned_flavor(workload)
                if flavor:
                    logger.info(
                        "Workload fournos-%s admitted to flavor %s", job_id, flavor
                    )
                    return flavor
            if asyncio.get_event_loop().time() >= deadline:
                break
            await asyncio.sleep(settings.admission_poll_interval_sec)

        raise TimeoutError(
            f"Workload fournos-{job_id} not admitted within "
            f"{settings.admission_poll_timeout_sec}s"
        )
