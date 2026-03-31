from __future__ import annotations

import logging

from kubernetes import client

from fournos.core.constants import LABEL_JOB_ID, LABEL_MANAGED_BY
from fournos.settings import settings

logger = logging.getLogger(__name__)

TEKTON_GROUP = "tekton.dev"
TEKTON_VERSION = "v1"
TEKTON_PIPELINE_RUN_PLURAL = "pipelineruns"


class TektonClient:
    def __init__(self, k8s_client: client.CustomObjectsApi) -> None:
        self._k8s = k8s_client

    def create_pipeline_run(
        self,
        *,
        job_id: str,
        job_name: str,
        pipeline: str,
        forge_project: str,
        forge_preset: str,
        forge_args: list[str],
        kubeconfig_secret: str,
        gpu_count: int,
        secrets: list[str],
        cluster: str,
    ) -> dict:
        pipeline_run_name = f"fournos-{job_id}"
        body = {
            "apiVersion": f"{TEKTON_GROUP}/{TEKTON_VERSION}",
            "kind": "PipelineRun",
            "metadata": {
                "name": pipeline_run_name,
                "namespace": settings.namespace,
                "labels": {
                    LABEL_MANAGED_BY: "fournos",
                    LABEL_JOB_ID: job_id,
                },
                "annotations": {
                    "fournos.dev/job-name": job_name,
                    "fournos.dev/cluster": cluster,
                },
            },
            "spec": {
                "pipelineRef": {"name": pipeline},
                "params": [
                    {"name": "job-id", "value": job_id},
                    {"name": "job-name", "value": job_name},
                    {"name": "forge-project", "value": forge_project},
                    {"name": "forge-preset", "value": forge_preset},
                    {"name": "forge-args", "value": forge_args},
                    {"name": "kubeconfig-secret", "value": kubeconfig_secret},
                    {"name": "gpu-count", "value": str(gpu_count)},
                    {"name": "secrets", "value": secrets},
                ],
            },
        }
        result = self._k8s.create_namespaced_custom_object(
            group=TEKTON_GROUP,
            version=TEKTON_VERSION,
            namespace=settings.namespace,
            plural=TEKTON_PIPELINE_RUN_PLURAL,
            body=body,
        )
        logger.info("Created PipelineRun %s for job %s", pipeline_run_name, job_id)
        return result

    def get_pipeline_run(self, job_id: str) -> dict:
        return self._k8s.get_namespaced_custom_object(
            group=TEKTON_GROUP,
            version=TEKTON_VERSION,
            namespace=settings.namespace,
            plural=TEKTON_PIPELINE_RUN_PLURAL,
            name=f"fournos-{job_id}",
        )

    def get_pipeline_run_or_none(self, job_id: str) -> dict | None:
        try:
            return self.get_pipeline_run(job_id)
        except client.exceptions.ApiException as exc:
            if exc.status == 404:
                return None
            raise

    def list_pipeline_runs(self) -> list[dict]:
        result = self._k8s.list_namespaced_custom_object(
            group=TEKTON_GROUP,
            version=TEKTON_VERSION,
            namespace=settings.namespace,
            plural=TEKTON_PIPELINE_RUN_PLURAL,
            label_selector=f"{LABEL_MANAGED_BY}=fournos",
        )
        return result.get("items", [])

    @staticmethod
    def extract_status(pr: dict) -> str:
        """Map PipelineRun conditions to: running, succeeded, or failed."""
        conditions = pr.get("status", {}).get("conditions", [])
        if not conditions:
            return "running"

        condition = conditions[-1]
        cond_status = condition.get("status", "Unknown")

        if cond_status == "True":
            return "succeeded"
        if cond_status == "False":
            return "failed"
        return "running"
