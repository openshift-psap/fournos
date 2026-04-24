from __future__ import annotations

import logging
import re
import shlex

import yaml

from kubernetes import client

from fournos.core.constants import LABEL_JOB_NAME, LABEL_MANAGED_BY
from fournos.settings import settings

logger = logging.getLogger(__name__)

TEKTON_GROUP = "tekton.dev"
TEKTON_VERSION = "v1"
TEKTON_PIPELINE_RUN_PLURAL = "pipelineruns"

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def serialize_env(env: dict) -> str:
    """Serialize env dict as ``KEY=quoted_value`` lines for ``source``.

    Keys are validated as shell identifiers so they cannot inject
    shell syntax.  Values are wrapped with :func:`shlex.quote` so
    ``source`` treats them as literals (no expansion or substitution).
    """
    lines: list[str] = []
    for key, value in env.items():
        if not _ENV_KEY_RE.match(key):
            raise ValueError(f"Invalid environment variable name: {key!r}")
        lines.append(f"{key}={shlex.quote(str(value))}\n")
    return "".join(lines)


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
        forge_config: dict,
        env: dict,
        kubeconfig_secret: str,
        gpu_count: int,
        secret_refs: list[str],
        cluster: str,
        owner_ref: dict | None = None,
    ) -> dict:
        labels = {
            LABEL_MANAGED_BY: "fournos",
            LABEL_JOB_NAME: name,
        }
        metadata: dict = {
            "name": name,
            "namespace": settings.namespace,
            "labels": labels,
            "annotations": {
                "fournos.dev/cluster": cluster,
            },
        }
        if owner_ref:
            metadata["ownerReferences"] = [owner_ref]

        body = {
            "apiVersion": f"{TEKTON_GROUP}/{TEKTON_VERSION}",
            "kind": "PipelineRun",
            "metadata": metadata,
            "spec": {
                "pipelineRef": {"name": pipeline},
                "taskRunTemplate": {
                    "metadata": {
                        "labels": labels,
                    },
                },
                "params": [
                    {"name": "job-name", "value": display_name},
                    {"name": "forge-project", "value": forge_project},
                    {
                        "name": "forge-config",
                        "value": yaml.dump(forge_config, default_flow_style=False),
                    },
                    {
                        "name": "env",
                        "value": serialize_env(env),
                    },
                    {"name": "kubeconfig-secret", "value": kubeconfig_secret},
                    {"name": "gpu-count", "value": str(gpu_count)},
                    {"name": "secret-refs", "value": secret_refs},
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
        logger.info("Created PipelineRun %s", name)
        return result

    def get_pipeline_run(self, name: str) -> dict:
        return self._k8s.get_namespaced_custom_object(
            group=TEKTON_GROUP,
            version=TEKTON_VERSION,
            namespace=settings.namespace,
            plural=TEKTON_PIPELINE_RUN_PLURAL,
            name=name,
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

    def cancel_pipeline_run(self, name: str, *, graceful: bool = True) -> None:
        """Cancel a PipelineRun. Ignores 404.

        *graceful=True* uses ``CancelledRunFinally`` (runs finally tasks).
        *graceful=False* uses ``Cancelled`` (skips finally tasks).
        """
        tekton_status = "CancelledRunFinally" if graceful else "Cancelled"
        try:
            self._k8s.patch_namespaced_custom_object(
                group=TEKTON_GROUP,
                version=TEKTON_VERSION,
                namespace=settings.namespace,
                plural=TEKTON_PIPELINE_RUN_PLURAL,
                name=name,
                body={"spec": {"status": tekton_status}},
            )
            logger.info("Set PipelineRun %s status to %s", name, tekton_status)
        except client.exceptions.ApiException as exc:
            if exc.status != 404:
                raise

    def delete_pipeline_run(self, name: str) -> None:
        """Delete the PipelineRun for *name*. Ignores 404."""
        try:
            self._k8s.delete_namespaced_custom_object(
                group=TEKTON_GROUP,
                version=TEKTON_VERSION,
                namespace=settings.namespace,
                plural=TEKTON_PIPELINE_RUN_PLURAL,
                name=name,
            )
            logger.info("Deleted PipelineRun %s", name)
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
