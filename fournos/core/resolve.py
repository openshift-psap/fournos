"""Resolve client — manages Forge resolve K8s Jobs."""

from __future__ import annotations

import copy
import logging
from pathlib import Path

import yaml
from kubernetes import client

from fournos.core.constants import LABEL_JOB_NAME, LABEL_MANAGED_BY
from fournos.settings import settings

logger = logging.getLogger(__name__)


def _load_job_template() -> dict:
    path = Path(settings.resolve_job_template)
    if not path.is_absolute():
        path = Path.cwd() / path
    return yaml.safe_load(path.read_text())


_RESOLVE_JOB_TEMPLATE: dict = _load_job_template()


def _resolve_job_name(name: str) -> str:
    return f"resolve-{name}"


def _make_owner_ref(ref: dict) -> dict:
    return {
        "apiVersion": ref["apiVersion"],
        "kind": ref["kind"],
        "name": ref["name"],
        "uid": ref["uid"],
        "controller": ref.get("controller", True),
        "blockOwnerDeletion": ref.get("blockOwnerDeletion", True),
    }


class ResolveClient:
    def __init__(self, batch_client: client.BatchV1Api) -> None:
        self._batch = batch_client

    def create_job(
        self,
        *,
        name: str,
        owner_ref: dict,
        image: str,
    ) -> dict:
        job_name = _resolve_job_name(name)
        labels = {LABEL_MANAGED_BY: "fournos", LABEL_JOB_NAME: name}

        body = copy.deepcopy(_RESOLVE_JOB_TEMPLATE)
        body["metadata"] = {
            "name": job_name,
            "namespace": settings.namespace,
            "labels": labels,
            "ownerReferences": [_make_owner_ref(owner_ref)],
        }
        body["spec"]["activeDeadlineSeconds"] = settings.resolve_deadline_sec
        body["spec"]["template"]["metadata"] = {"labels": labels}

        container = body["spec"]["template"]["spec"]["containers"][0]
        container["image"] = image

        env_values = {
            "FJOB_NAME": name,
            "FOURNOS_NAMESPACE": settings.namespace,
        }
        for env_var in container["env"]:
            if env_var["name"] not in env_values:
                continue
            env_var["value"] = env_values[env_var["name"]]

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
