from __future__ import annotations

import json
import logging

from kubernetes import client

from fournos.core.constants import LABEL_JOB_NAME, LABEL_MANAGED_BY
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
        name: str,
        display_name: str,
        pipeline: str,
        forge_project: str,
        forge_preset: str,
        forge_config_overrides: dict,
        env: dict,
        kubeconfig_secret: str,
        gpu_count: int,
        secrets: list[str],
        cluster: str,
    ) -> dict:
        pipeline_run_name = f"fournos-{name}"
        body = {
            "apiVersion": f"{TEKTON_GROUP}/{TEKTON_VERSION}",
            "kind": "PipelineRun",
            "metadata": {
                "name": pipeline_run_name,
                "namespace": settings.namespace,
                "labels": {
                    LABEL_MANAGED_BY: "fournos",
                    LABEL_JOB_NAME: name,
                },
                "annotations": {
                    "fournos.dev/cluster": cluster,
                },
            },
            "spec": {
                "pipelineRef": {"name": pipeline},
                "params": [
                    {"name": "job-name", "value": display_name},
                    {"name": "forge-project", "value": forge_project},
                    {"name": "forge-preset", "value": forge_preset},
                    {
                        "name": "forge-config-overrides",
                        "value": json.dumps(forge_config_overrides),
                    },
                    {"name": "env", "value": json.dumps(env)},
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
        logger.info("Created PipelineRun %s for job %s", pipeline_run_name, name)
        return result

    def get_pipeline_run(self, name: str) -> dict:
        return self._k8s.get_namespaced_custom_object(
            group=TEKTON_GROUP,
            version=TEKTON_VERSION,
            namespace=settings.namespace,
            plural=TEKTON_PIPELINE_RUN_PLURAL,
            name=f"fournos-{name}",
        )

    def get_pipeline_run_or_none(self, name: str) -> dict | None:
        try:
            return self.get_pipeline_run(name)
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

    def delete_pipeline_run(self, name: str) -> None:
        """Delete the PipelineRun for *name*. Ignores 404."""
        pipeline_run_name = f"fournos-{name}"
        try:
            self._k8s.delete_namespaced_custom_object(
                group=TEKTON_GROUP,
                version=TEKTON_VERSION,
                namespace=settings.namespace,
                plural=TEKTON_PIPELINE_RUN_PLURAL,
                name=pipeline_run_name,
            )
            logger.info("Deleted PipelineRun %s", pipeline_run_name)
        except client.exceptions.ApiException as exc:
            if exc.status != 404:
                raise

    @staticmethod
    def extract_status(pr: dict) -> tuple[str, str]:
        """Map PipelineRun conditions to (status, message).

        Status is one of: running, succeeded, failed.

        A PipelineRun is only considered terminal once
        status.completionTime is set by the Tekton controller.
        """
        completed = pr.get("status", {}).get("completionTime") is not None

        conditions = pr.get("status", {}).get("conditions", [])
        if not conditions:
            return "running", ""

        condition = conditions[-1]
        cond_status = condition.get("status", "Unknown")
        message = condition.get("message", "")

        if cond_status == "True":
            return "succeeded", message
        if cond_status == "False" and completed:
            return "failed", message
        return "running", message
