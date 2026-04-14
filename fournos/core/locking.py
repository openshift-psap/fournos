"""Cluster-lock queries — determine which clusters are exclusively locked or occupied."""

from __future__ import annotations

from kubernetes import client

from fournos.core.constants import LABEL_EXCLUSIVE_CLUSTER, TERMINAL_PHASES
from fournos.settings import settings

CRD_GROUP = "fournos.dev"
CRD_VERSION = "v1"


def get_locked_clusters() -> dict[str, str]:
    """Return ``{cluster_name: job_name}`` for every active exclusive lock.

    A cluster is locked when a non-terminal FournosJob carries the
    ``fournos.dev/exclusive-cluster`` label.
    """
    custom = client.CustomObjectsApi()
    jobs = custom.list_namespaced_custom_object(
        CRD_GROUP,
        CRD_VERSION,
        settings.namespace,
        "fournosjobs",
        label_selector=LABEL_EXCLUSIVE_CLUSTER,
    )
    locks: dict[str, str] = {}
    for job in jobs.get("items", []):
        phase = job.get("status", {}).get("phase", "")
        if phase and phase in TERMINAL_PHASES:
            continue
        cluster = (
            job.get("metadata", {}).get("labels", {}).get(LABEL_EXCLUSIVE_CLUSTER, "")
        )
        if cluster:
            locks[cluster] = job["metadata"]["name"]
    return locks


def is_cluster_occupied(cluster: str, exclude_job: str) -> list[str]:
    """Return names of non-terminal jobs running on *cluster* (excluding *exclude_job*)."""
    custom = client.CustomObjectsApi()
    jobs = custom.list_namespaced_custom_object(
        CRD_GROUP,
        CRD_VERSION,
        settings.namespace,
        "fournosjobs",
    )
    active_jobs: list[str] = []
    for job in jobs.get("items", []):
        job_name = job["metadata"]["name"]
        if job_name == exclude_job:
            continue
        phase = job.get("status", {}).get("phase", "")
        if phase in TERMINAL_PHASES:
            continue
        spec_cluster = job.get("spec", {}).get("cluster")
        status_cluster = job.get("status", {}).get("cluster")
        if cluster in (spec_cluster, status_cluster):
            active_jobs.append(job_name)
    return active_jobs
