from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import yaml
from kubernetes import client, config as k8s_config

from fournos.settings import settings

logger = logging.getLogger(__name__)


GPU_MODEL_MAP: dict[str, str] = {
    "NVIDIA-A100-SXM4-80GB": "a100",
    "NVIDIA-A100-SXM4-40GB": "a100",
    "NVIDIA-A100-PCIE-80GB": "a100",
    "NVIDIA-A100-PCIE-40GB": "a100",
    "NVIDIA-H100-SXM5-80GB": "h100",
    "NVIDIA-H100-PCIE-80GB": "h100",
    "NVIDIA-H200-SXM-141GB": "h200",
    "AMD-Instinct-MI300X": "mi300x",
}

GPU_RESOURCE_NVIDIA = "nvidia.com/gpu"
GPU_RESOURCE_AMD = "amd.com/gpu"
GPU_LABEL_NVIDIA = "nvidia.com/gpu.product"
GPU_LABEL_AMD = "amd.com/gpu.product"


class GPUDiscoveryError(Exception):
    pass


@dataclass(frozen=True)
class DiscoveredGPU:
    vendor: str
    model: str
    short_name: str
    count: int
    node_count: int


@dataclass(frozen=True)
class DiscoveryResult:
    gpus: tuple[DiscoveredGPU, ...]
    total_gpus: int
    timestamp: str


def _normalize_gpu_model(raw_model: str) -> str:
    if raw_model in GPU_MODEL_MAP:
        return GPU_MODEL_MAP[raw_model]
    for known, short in GPU_MODEL_MAP.items():
        base = "-".join(known.split("-")[:2])
        if raw_model.startswith(base):
            return short
    return raw_model.lower().replace(" ", "-")


class GPUDiscoveryClient:
    def __init__(self, management_core: client.CoreV1Api) -> None:
        self._management_core = management_core

    def _read_kubeconfig_from_secret(
        self, secret_name: str, namespace: str
    ) -> dict:
        try:
            secret = self._management_core.read_namespaced_secret(
                secret_name, namespace
            )
        except client.exceptions.ApiException as exc:
            if exc.status == 404:
                raise GPUDiscoveryError(
                    f"Kubeconfig secret {secret_name!r} not found in {namespace}"
                ) from exc
            raise

        raw = None
        if secret.data and "kubeconfig" in secret.data:
            raw = base64.b64decode(secret.data["kubeconfig"]).decode()
        elif secret.string_data and "kubeconfig" in secret.string_data:
            raw = secret.string_data["kubeconfig"]

        if not raw:
            raise GPUDiscoveryError(
                f"Secret {secret_name!r} has no 'kubeconfig' key"
            )

        parsed = yaml.safe_load(raw)

        if isinstance(parsed, str):
            try:
                parsed = yaml.safe_load(base64.b64decode(parsed).decode())
            except Exception:
                pass

        if not isinstance(parsed, dict):
            raise GPUDiscoveryError(
                f"Secret {secret_name!r} kubeconfig is not valid YAML"
            )
        return parsed

    def discover_gpus(
        self,
        cluster_name: str,
        kubeconfig_secret: str,
        secrets_namespace: str,
    ) -> DiscoveryResult:
        kubeconfig_dict = self._read_kubeconfig_from_secret(
            kubeconfig_secret, secrets_namespace
        )

        if "current-context" not in kubeconfig_dict:
            contexts = kubeconfig_dict.get("contexts", [])
            if contexts:
                kubeconfig_dict["current-context"] = contexts[0]["name"]
            else:
                raise GPUDiscoveryError(
                    f"Kubeconfig for {cluster_name!r} has no contexts"
                )

        api_client = k8s_config.new_client_from_config_dict(kubeconfig_dict)
        api_client.configuration.request_timeout = (
            settings.gpu_discovery_timeout_sec,
            settings.gpu_discovery_timeout_sec,
        )

        try:
            target_core = client.CoreV1Api(api_client)
            nodes = target_core.list_node()
        except Exception as exc:
            raise GPUDiscoveryError(
                f"Failed to connect to cluster {cluster_name!r}: {exc}"
            ) from exc
        finally:
            api_client.close()

        gpu_counts: dict[tuple[str, str, str], list[int]] = {}

        for node in nodes.items:
            allocatable = node.status.allocatable or {}
            labels = node.metadata.labels or {}

            nvidia_count = int(allocatable.get(GPU_RESOURCE_NVIDIA, 0))
            amd_count = int(allocatable.get(GPU_RESOURCE_AMD, 0))

            if nvidia_count > 0:
                raw_model = labels.get(GPU_LABEL_NVIDIA, "unknown-nvidia")
                short = _normalize_gpu_model(raw_model)
                key = ("nvidia", raw_model, short)
                gpu_counts.setdefault(key, []).append(nvidia_count)

            if amd_count > 0:
                raw_model = labels.get(GPU_LABEL_AMD, "unknown-amd")
                short = _normalize_gpu_model(raw_model)
                key = ("amd", raw_model, short)
                gpu_counts.setdefault(key, []).append(amd_count)

        gpus = tuple(
            DiscoveredGPU(
                vendor=vendor,
                model=model,
                short_name=short,
                count=sum(counts),
                node_count=len(counts),
            )
            for (vendor, model, short), counts in sorted(gpu_counts.items())
        )

        total = sum(g.count for g in gpus)
        timestamp = datetime.now(timezone.utc).isoformat()

        logger.info(
            "Discovered %d GPUs on cluster %s: %s",
            total,
            cluster_name,
            ", ".join(f"{g.count}x {g.short_name}" for g in gpus) or "none",
        )

        return DiscoveryResult(gpus=gpus, total_gpus=total, timestamp=timestamp)
