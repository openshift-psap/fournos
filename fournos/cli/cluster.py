"""The cluster adapter — ClusterClient port and its Kubernetes implementation.

Everything the CLI needs from the cluster runs on the hub: FournosJob CRs
(fournos.dev/v1) and the execution pods Tekton creates there. Pod lookup is
label-driven (specs/cluster-resources.allium): pods are selected by the
``fournos.dev/job-name`` label (Tekton overwrites managed-by on the pods it
creates) and exclude Tekton's affinity-assistant pods. Owner filtering is not
part of the selector: it resolves the effective owner per pod (label ?? the
owning FournosJob's owner), which the command layer does.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Protocol

import urllib3
from kubernetes import client

from fournos.core.constants import (
    LABEL_JOB_NAME,
    LABEL_OWNER,
    Shutdown,
)

from .model import ExecutionPod, FJob, JobPhase, PodPhase

logger = logging.getLogger(__name__)

FJOB_GROUP = "fournos.dev"
FJOB_VERSION = "v1"
FJOB_PLURAL = "fournosjobs"


class ClusterClient(Protocol):
    """The port command logic talks to; ``FakeCluster`` and ``KubeClusterClient`` implement it."""

    def list_fjobs(self, namespace: str) -> list[FJob]: ...

    def list_pods(
        self,
        namespace: str,
        fjob_name: str | None = None,
    ) -> list[ExecutionPod]: ...

    def get_fjob(self, namespace: str, name: str) -> FJob | None: ...

    def set_shutdown(self, namespace: str, name: str, mode: Shutdown) -> None: ...

    def delete_fjob(self, namespace: str, name: str) -> None: ...


def _parse_timestamp(value) -> datetime | None:
    """Accept datetimes (typed API objects) and RFC3339 strings (custom objects)."""
    if value is None or isinstance(value, datetime):
        return value
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _as_dict(obj) -> dict:
    """Normalise typed API objects and plain dicts to a plain dict."""
    return obj if isinstance(obj, dict) else obj.to_dict()


class KubeClusterClient:
    """ClusterClient over the Kubernetes APIs (FournosJob CRs + execution pods)."""

    def __init__(
        self,
        kubeconfig: str | None = None,
        *,
        custom_objects: client.CustomObjectsApi | None = None,
        core: client.CoreV1Api | None = None,
    ) -> None:
        self._kubeconfig = kubeconfig
        self._custom_objects = custom_objects
        self._core = core
        self._custom_injected = custom_objects is not None
        self._config_loaded = False

    # -- API object plumbing ---------------------------------------------------

    def _ensure_config(self) -> None:
        if self._config_loaded:
            return
        from kubernetes import config as k8s_config

        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException:
            configuration = client.Configuration()
            k8s_config.load_kube_config(
                config_file=self._kubeconfig, client_configuration=configuration
            )
            client.Configuration.set_default(configuration)
            if not configuration.verify_ssl:
                # The kubeconfig context explicitly opted out of TLS
                # verification (insecure-skip-tls-verify: true) — kubectl
                # stays quiet about it, so neither should the CLI.
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self._config_loaded = True

    def _custom_api(self):
        if self._custom_objects is None:
            self._ensure_config()
            self._custom_objects = client.CustomObjectsApi()
        return self._custom_objects

    def _core_api(self):
        if self._core is None:
            if self._custom_injected:
                # A caller-injected object backs both halves of the port
                # (tests inject a single recording API).
                return self._custom_objects
            self._ensure_config()
            self._core = client.CoreV1Api()
        return self._core

    # -- ClusterClient port ----------------------------------------------------

    def list_fjobs(self, namespace: str) -> list[FJob]:
        result = self._custom_api().list_namespaced_custom_object(
            group=FJOB_GROUP,
            version=FJOB_VERSION,
            namespace=namespace,
            plural=FJOB_PLURAL,
        )
        return [self._map_fjob(item) for item in result.get("items", [])]

    def list_pods(
        self,
        namespace: str,
        fjob_name: str | None = None,
    ) -> list[ExecutionPod]:
        result = self._core_api().list_namespaced_pod(
            namespace=namespace,
            label_selector=self._pod_selector(fjob_name),
        )
        items = result.get("items", []) if isinstance(result, dict) else result.items
        return [self._map_pod(item) for item in items]

    def get_fjob(self, namespace: str, name: str) -> FJob | None:
        try:
            cr = self._custom_api().get_namespaced_custom_object(
                group=FJOB_GROUP,
                version=FJOB_VERSION,
                namespace=namespace,
                plural=FJOB_PLURAL,
                name=name,
            )
        except client.exceptions.ApiException as exc:
            if exc.status == 404:
                return None
            raise
        return self._map_fjob(cr)

    def set_shutdown(self, namespace: str, name: str, mode: Shutdown) -> None:
        self._custom_api().patch_namespaced_custom_object(
            group=FJOB_GROUP,
            version=FJOB_VERSION,
            namespace=namespace,
            plural=FJOB_PLURAL,
            name=name,
            body={"spec": {"shutdown": mode.value}},
        )

    def delete_fjob(self, namespace: str, name: str) -> None:
        self._custom_api().delete_namespaced_custom_object(
            group=FJOB_GROUP,
            version=FJOB_VERSION,
            namespace=namespace,
            plural=FJOB_PLURAL,
            name=name,
        )

    # -- log streaming (used by the CLI after LogStream opens) -----------------

    def read_pod_logs(self, namespace: str, name: str, *, follow: bool = False):
        """Stream pod logs; returns an iterable of decoded lines/chunks."""
        response = self._core_api().read_namespaced_pod_log(
            name=name,
            namespace=namespace,
            follow=follow,
            _preload_content=False,
        )
        return response

    # -- mapping ---------------------------------------------------------------

    @staticmethod
    def _pod_selector(fjob_name: str | None) -> str:
        # Tekton force-overwrites app.kubernetes.io/managed-by on the pods it
        # creates, so execution pods are identified by the fournos.dev/job-name
        # label instead — stamped on the taskRunTemplate (inherited by TaskRun
        # pods) and on the resolve Job's pod template.
        if fjob_name is None:
            parts = [LABEL_JOB_NAME]  # key-existence: any fournos pod
        else:
            parts = [f"{LABEL_JOB_NAME}={fjob_name}"]
        # Tekton's affinity-assistant StatefulSet pods inherit the job-name
        # label from the PipelineRun but are volume-affinity infrastructure,
        # not execution pods — excluded by their component label.
        parts.append("app.kubernetes.io/component!=affinity-assistant")
        return ",".join(parts)

    @staticmethod
    def _map_fjob(cr: dict) -> FJob:
        meta = cr.get("metadata", {}) or {}
        spec = cr.get("spec", {}) or {}
        status = cr.get("status", {}) or {}
        phase = status.get("phase")
        shutdown = spec.get("shutdown")
        return FJob(
            name=meta.get("name", ""),
            namespace=meta.get("namespace", ""),
            owner=spec.get("owner"),
            phase=JobPhase(phase) if phase else JobPhase.SCHEDULED,
            cluster=status.get("cluster"),
            pipeline_run=status.get("pipelineRun"),
            dashboard_url=status.get("dashboardURL"),
            created_at=_parse_timestamp(meta.get("creationTimestamp")),
            shutdown=Shutdown(shutdown) if shutdown else None,
        )

    @staticmethod
    def _map_pod(obj) -> ExecutionPod:
        pod = _as_dict(obj)
        meta = pod.get("metadata", {}) or {}
        labels = meta.get("labels") or {}
        status = pod.get("status", {}) or {}
        restarts = sum(
            (container or {}).get("restartCount") or 0
            for container in status.get("containerStatuses") or []
        )
        return ExecutionPod(
            name=meta.get("name", ""),
            namespace=meta.get("namespace", ""),
            job_name=labels.get(LABEL_JOB_NAME),
            owner_label=labels.get(LABEL_OWNER),
            status=PodPhase(status.get("phase", PodPhase.PENDING)),
            started_at=_parse_timestamp(status.get("startTime")),
            restarts=restarts,
        )
