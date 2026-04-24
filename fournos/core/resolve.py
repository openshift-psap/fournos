"""Resolve client — manages Forge resolve K8s Jobs and FournosJobConfig CRs."""

from __future__ import annotations

import logging

import yaml
from kubernetes import client

from fournos.core.constants import LABEL_JOB_NAME, LABEL_MANAGED_BY
from fournos.settings import settings

logger = logging.getLogger(__name__)

FJOBCONFIG_GROUP = "fournos.dev"
FJOBCONFIG_VERSION = "v1"
FJOBCONFIG_PLURAL = "fournosjobconfigs"


def _resolve_job_name(name: str) -> str:
    return f"resolve-{name}"


class ResolveClient:
    def __init__(
        self,
        batch_client: client.BatchV1Api,
        custom_client: client.CustomObjectsApi,
    ) -> None:
        self._batch = batch_client
        self._custom = custom_client

    def create_fournos_job_config(self, *, name: str, owner_ref: dict) -> None:
        """Create an empty FournosJobConfig for Forge to populate.

        Idempotent: a 409 (AlreadyExists) is silently ignored.
        """
        config_name = _resolve_job_name(name)
        body = {
            "apiVersion": f"{FJOBCONFIG_GROUP}/{FJOBCONFIG_VERSION}",
            "kind": "FournosJobConfig",
            "metadata": {
                "name": config_name,
                "namespace": settings.namespace,
                "labels": {
                    LABEL_MANAGED_BY: "fournos",
                    LABEL_JOB_NAME: name,
                },
                "ownerReferences": [
                    {
                        "apiVersion": owner_ref["apiVersion"],
                        "kind": owner_ref["kind"],
                        "name": owner_ref["name"],
                        "uid": owner_ref["uid"],
                        "controller": owner_ref.get("controller", True),
                        "blockOwnerDeletion": owner_ref.get("blockOwnerDeletion", True),
                    }
                ],
            },
            "spec": {},
        }
        try:
            self._custom.create_namespaced_custom_object(
                group=FJOBCONFIG_GROUP,
                version=FJOBCONFIG_VERSION,
                namespace=settings.namespace,
                plural=FJOBCONFIG_PLURAL,
                body=body,
            )
            logger.info(
                "Created empty FournosJobConfig %s for job %s", config_name, name
            )
        except client.exceptions.ApiException as exc:
            if exc.status == 409:
                logger.debug("FournosJobConfig %s already exists (409)", config_name)
            else:
                raise

    def create_job(
        self,
        *,
        name: str,
        forge_project: str,
        forge_config: dict,
        owner_ref: dict,
    ) -> dict:
        job_name = _resolve_job_name(name)
        body = client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=client.V1ObjectMeta(
                name=job_name,
                namespace=settings.namespace,
                labels={
                    LABEL_MANAGED_BY: "fournos",
                    LABEL_JOB_NAME: name,
                },
                owner_references=[
                    client.V1OwnerReference(
                        api_version=owner_ref["apiVersion"],
                        kind=owner_ref["kind"],
                        name=owner_ref["name"],
                        uid=owner_ref["uid"],
                        controller=owner_ref.get("controller", True),
                        block_owner_deletion=owner_ref.get("blockOwnerDeletion", True),
                    ),
                ],
            ),
            spec=client.V1JobSpec(
                backoff_limit=0,
                active_deadline_seconds=settings.forge_resolve_deadline_sec,
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(
                        labels={
                            LABEL_MANAGED_BY: "fournos",
                            LABEL_JOB_NAME: name,
                        },
                    ),
                    spec=client.V1PodSpec(
                        service_account_name="fournos",
                        restart_policy="Never",
                        containers=[
                            client.V1Container(
                                name="resolve",
                                image=settings.forge_resolve_image.format(
                                    namespace=settings.namespace,
                                ),
                                image_pull_policy="IfNotPresent",
                                env=[
                                    client.V1EnvVar(
                                        name="FOURNOS_JOB_NAME",
                                        value=name,
                                    ),
                                    client.V1EnvVar(
                                        name="FOURNOS_NAMESPACE",
                                        value=settings.namespace,
                                    ),
                                    client.V1EnvVar(
                                        name="FOURNOS_CONFIG_NAME",
                                        value=_resolve_job_name(name),
                                    ),
                                    client.V1EnvVar(
                                        name="FORGE_PROJECT",
                                        value=forge_project,
                                    ),
                                    client.V1EnvVar(
                                        name="FORGE_CONFIG",
                                        value=yaml.dump(
                                            forge_config,
                                            default_flow_style=False,
                                        ),
                                    ),
                                ],
                            ),
                        ],
                    ),
                ),
            ),
        )
        result = self._batch.create_namespaced_job(
            namespace=settings.namespace,
            body=body,
        )
        logger.info("Created resolve Job %s for job %s", job_name, name)
        return result.to_dict()

    def get_job_or_none(self, name: str) -> dict | None:
        job_name = _resolve_job_name(name)
        try:
            result = self._batch.read_namespaced_job(
                name=job_name,
                namespace=settings.namespace,
            )
            return result.to_dict()
        except client.exceptions.ApiException as exc:
            if exc.status == 404:
                return None
            raise

    @staticmethod
    def get_job_status(job: dict) -> str:
        """Return 'running', 'succeeded', or 'failed' from Job conditions."""
        conditions = job.get("status", {}).get("conditions") or []
        for c in conditions:
            ctype = c.get("type", "")
            cstatus = c.get("status", "")
            if ctype == "Complete" and cstatus == "True":
                return "succeeded"
            if ctype == "Failed" and cstatus == "True":
                return "failed"
        return "running"

    @staticmethod
    def get_job_message(job: dict) -> str:
        """Extract a human-readable message from a failed Job."""
        conditions = job.get("status", {}).get("conditions") or []
        for c in conditions:
            if c.get("type") == "Failed" and c.get("message"):
                return c["message"]
        return ""

    def read_job_config(self, name: str) -> dict | None:
        """Read a FournosJobConfig CR. Returns spec dict or None."""
        config_name = _resolve_job_name(name)
        try:
            result = self._custom.get_namespaced_custom_object(
                group=FJOBCONFIG_GROUP,
                version=FJOBCONFIG_VERSION,
                namespace=settings.namespace,
                plural=FJOBCONFIG_PLURAL,
                name=config_name,
            )
            return result.get("spec", {})
        except client.exceptions.ApiException as exc:
            if exc.status == 404:
                return None
            raise
