from __future__ import annotations

import logging

from kubernetes import client

from fournos.core.clusters import ResolvedSecret
from fournos.core.constants import LABEL_JOB_NAME, LABEL_MANAGED_BY
from fournos.settings import settings

logger = logging.getLogger(__name__)

TEKTON_GROUP = "tekton.dev"
TEKTON_VERSION = "v1"
TEKTON_PIPELINE_PLURAL = "pipelines"
TEKTON_PIPELINE_RUN_PLURAL = "pipelineruns"

ANNOTATION_RESOLVE_IMAGE = "fournos.dev/resolve-image"


def _build_secrets_volume(resolved: list[ResolvedSecret]) -> dict:
    """Build a single projected volume combining all per-job secret copies.

    Always returns a valid volume spec -- when *resolved* is empty the
    ``sources`` list is empty, which produces an empty directory so
    static volumeMounts in Task YAMLs remain valid.
    """
    return {
        "name": "vault-secrets",
        "projected": {
            "sources": [
                {
                    "secret": {
                        "name": r.name,
                        "items": [
                            {"key": k, "path": f"{r.original_name}/{k}"} for k in r.keys
                        ],
                    },
                }
                for r in resolved
            ],
        },
    }


class TektonClient:
    def __init__(self, k8s_client: client.CustomObjectsApi) -> None:
        self._k8s = k8s_client

    def create_pipeline_run(
        self,
        *,
        name: str,
        pipeline: str,
        kubeconfig_secret: str,
        resolved_secrets: list[ResolvedSecret],
        cluster: str,
        owner_ref: dict | None = None,
    ) -> dict:
        labels = {
            LABEL_MANAGED_BY: "fournos",
            LABEL_JOB_NAME: name,
        }
        metadata: dict = {
            "name": name,
            "namespace": settings.workload_namespace,
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
                    "podTemplate": {
                        "volumes": [_build_secrets_volume(resolved_secrets)],
                    },
                },
                "params": [
                    {"name": "fjob-name", "value": name},
                    {
                        "name": "fournos-workload-namespace",
                        "value": settings.workload_namespace,
                    },
                    {"name": "kubeconfig-secret", "value": kubeconfig_secret},
                ],
                "workspaces": [
                    {
                        "name": "artifacts",
                        "volumeClaimTemplate": {
                            "metadata": {
                                "labels": labels,
                            },
                            "spec": {
                                "accessModes": ["ReadWriteOnce"],
                                "resources": {
                                    "requests": {
                                        "storage": settings.artifact_pvc_size,
                                    },
                                },
                            },
                        },
                    },
                ],
            },
        }
        result = self._k8s.create_namespaced_custom_object(
            group=TEKTON_GROUP,
            version=TEKTON_VERSION,
            namespace=settings.workload_namespace,
            plural=TEKTON_PIPELINE_RUN_PLURAL,
            body=body,
        )
        logger.info("Created PipelineRun %s", name)
        return result

    def get_pipeline(self, name: str) -> dict:
        return self._k8s.get_namespaced_custom_object(
            group=TEKTON_GROUP,
            version=TEKTON_VERSION,
            namespace=settings.workload_namespace,
            plural=TEKTON_PIPELINE_PLURAL,
            name=name,
        )

    def get_pipeline_run(self, name: str) -> dict:
        return self._k8s.get_namespaced_custom_object(
            group=TEKTON_GROUP,
            version=TEKTON_VERSION,
            namespace=settings.workload_namespace,
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
            namespace=settings.workload_namespace,
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
                namespace=settings.workload_namespace,
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
                namespace=settings.workload_namespace,
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
