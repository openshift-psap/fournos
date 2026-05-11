"""Tests for GPU discovery client."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.exceptions import ApiException

from fournos_cluster.core.gpu_discovery import (
    GPUDiscoveryClient,
    GPUDiscoveryError,
    DiscoveredGPU,
    DiscoveryResult,
    _normalize_gpu_model,
)


class TestNormalizeGPUModel:

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("NVIDIA-A100-SXM4-80GB", "a100"),
            ("NVIDIA-A100-SXM4-40GB", "a100"),
            ("NVIDIA-A100-PCIE-80GB", "a100"),
            ("NVIDIA-H100-SXM5-80GB", "h100"),
            ("NVIDIA-H200-SXM-141GB", "h200"),
            ("AMD-Instinct-MI300X", "mi300x"),
        ],
    )
    def test_known_models(self, raw: str, expected: str) -> None:
        assert _normalize_gpu_model(raw) == expected

    def test_prefix_fallback(self) -> None:
        result = _normalize_gpu_model("NVIDIA-A100-NEW-VARIANT-96GB")
        assert result == "a100"

    def test_unknown_model_lowercased(self) -> None:
        result = _normalize_gpu_model("SOME-UNKNOWN-GPU")
        assert result == "some-unknown-gpu"

    def test_unknown_with_spaces(self) -> None:
        result = _normalize_gpu_model("NVIDIA Tesla V100")
        assert result == "nvidia-tesla-v100"


class TestGPUDiscoveryClient:

    def _make_client(self) -> tuple[GPUDiscoveryClient, MagicMock]:
        mock_core = MagicMock()
        return GPUDiscoveryClient(mock_core), mock_core

    def _make_node(
        self,
        name: str,
        nvidia_gpus: int = 0,
        amd_gpus: int = 0,
        nvidia_model: str = "NVIDIA-A100-SXM4-80GB",
        amd_model: str = "AMD-Instinct-MI300X",
    ) -> MagicMock:
        node = MagicMock()
        node.metadata.name = name
        allocatable = {}
        labels = {}

        if nvidia_gpus > 0:
            allocatable["nvidia.com/gpu"] = str(nvidia_gpus)
            labels["nvidia.com/gpu.product"] = nvidia_model
        if amd_gpus > 0:
            allocatable["amd.com/gpu"] = str(amd_gpus)
            labels["amd.com/gpu.product"] = amd_model

        node.status.allocatable = allocatable
        node.metadata.labels = labels
        return node

    def _mock_kubeconfig_secret(self, mock_core: MagicMock) -> None:
        import base64
        kubeconfig = (
            "apiVersion: v1\nkind: Config\n"
            "current-context: default\n"
            "contexts:\n- name: default\n  context:\n    cluster: c1\n"
            "clusters: []\nusers: []\n"
        )
        secret = MagicMock()
        secret.data = {"kubeconfig": base64.b64encode(kubeconfig.encode()).decode()}
        secret.string_data = None
        mock_core.read_namespaced_secret.return_value = secret

    @patch("fournos_cluster.core.gpu_discovery.k8s_config")
    @patch("fournos_cluster.core.gpu_discovery.client")
    def test_discover_single_gpu_type(
        self, mock_k8s_client: MagicMock, mock_k8s_config: MagicMock
    ) -> None:
        discovery, mock_core = self._make_client()
        self._mock_kubeconfig_secret(mock_core)

        nodes = MagicMock()
        nodes.items = [
            self._make_node("node-1", nvidia_gpus=4),
            self._make_node("node-2", nvidia_gpus=4),
        ]

        mock_api_client = MagicMock()
        mock_k8s_config.new_client_from_config_dict.return_value = mock_api_client
        mock_k8s_client.CoreV1Api.return_value.list_node.return_value = nodes

        result = discovery.discover_gpus("cluster-1", "kubeconfig-cluster-1", "psap-secrets")

        assert result.total_gpus == 8
        assert len(result.gpus) == 1
        assert result.gpus[0].short_name == "a100"
        assert result.gpus[0].count == 8
        assert result.gpus[0].node_count == 2
        assert result.gpus[0].vendor == "nvidia"
        mock_api_client.close.assert_called_once()

    @patch("fournos_cluster.core.gpu_discovery.k8s_config")
    @patch("fournos_cluster.core.gpu_discovery.client")
    def test_discover_mixed_gpu_types(
        self, mock_k8s_client: MagicMock, mock_k8s_config: MagicMock
    ) -> None:
        discovery, mock_core = self._make_client()
        self._mock_kubeconfig_secret(mock_core)

        nodes = MagicMock()
        nodes.items = [
            self._make_node("node-1", nvidia_gpus=4, nvidia_model="NVIDIA-A100-SXM4-80GB"),
            self._make_node("node-2", nvidia_gpus=8, nvidia_model="NVIDIA-H200-SXM-141GB"),
        ]

        mock_api_client = MagicMock()
        mock_k8s_config.new_client_from_config_dict.return_value = mock_api_client
        mock_k8s_client.CoreV1Api.return_value.list_node.return_value = nodes

        result = discovery.discover_gpus("cluster-1", "kubeconfig-cluster-1", "psap-secrets")

        assert result.total_gpus == 12
        assert len(result.gpus) == 2
        short_names = {g.short_name for g in result.gpus}
        assert short_names == {"a100", "h200"}

    @patch("fournos_cluster.core.gpu_discovery.k8s_config")
    @patch("fournos_cluster.core.gpu_discovery.client")
    def test_discover_no_gpus(
        self, mock_k8s_client: MagicMock, mock_k8s_config: MagicMock
    ) -> None:
        discovery, mock_core = self._make_client()
        self._mock_kubeconfig_secret(mock_core)

        nodes = MagicMock()
        nodes.items = [self._make_node("node-1")]

        mock_api_client = MagicMock()
        mock_k8s_config.new_client_from_config_dict.return_value = mock_api_client
        mock_k8s_client.CoreV1Api.return_value.list_node.return_value = nodes

        result = discovery.discover_gpus("cluster-1", "kubeconfig-cluster-1", "psap-secrets")

        assert result.total_gpus == 0
        assert len(result.gpus) == 0

    @patch("fournos_cluster.core.gpu_discovery.k8s_config")
    @patch("fournos_cluster.core.gpu_discovery.client")
    def test_discover_amd_gpus(
        self, mock_k8s_client: MagicMock, mock_k8s_config: MagicMock
    ) -> None:
        discovery, mock_core = self._make_client()
        self._mock_kubeconfig_secret(mock_core)

        nodes = MagicMock()
        nodes.items = [self._make_node("node-1", amd_gpus=8)]

        mock_api_client = MagicMock()
        mock_k8s_config.new_client_from_config_dict.return_value = mock_api_client
        mock_k8s_client.CoreV1Api.return_value.list_node.return_value = nodes

        result = discovery.discover_gpus("mi300x", "kubeconfig-mi300x", "psap-secrets")

        assert result.total_gpus == 8
        assert result.gpus[0].vendor == "amd"
        assert result.gpus[0].short_name == "mi300x"

    def test_kubeconfig_secret_not_found(self) -> None:
        discovery, mock_core = self._make_client()
        mock_core.read_namespaced_secret.side_effect = ApiException(status=404)

        with pytest.raises(GPUDiscoveryError, match="not found"):
            discovery.discover_gpus("cluster-1", "kubeconfig-cluster-1", "psap-secrets")

    def test_kubeconfig_secret_no_key(self) -> None:
        discovery, mock_core = self._make_client()
        secret = MagicMock()
        secret.data = {"other-key": "value"}
        secret.string_data = None
        mock_core.read_namespaced_secret.return_value = secret

        with pytest.raises(GPUDiscoveryError, match="no 'kubeconfig' key"):
            discovery.discover_gpus("cluster-1", "kubeconfig-cluster-1", "psap-secrets")

    @patch("fournos_cluster.core.gpu_discovery.k8s_config")
    @patch("fournos_cluster.core.gpu_discovery.client")
    def test_double_base64_kubeconfig(
        self, mock_k8s_client: MagicMock, mock_k8s_config: MagicMock
    ) -> None:
        import base64 as b64

        discovery, mock_core = self._make_client()

        kubeconfig = (
            "apiVersion: v1\nkind: Config\n"
            "current-context: default\n"
            "contexts:\n- name: default\n  context:\n    cluster: c1\n"
            "clusters: []\nusers: []\n"
        )
        double_encoded = b64.b64encode(
            b64.b64encode(kubeconfig.encode())
        ).decode()

        secret = MagicMock()
        secret.data = {"kubeconfig": double_encoded}
        secret.string_data = None
        mock_core.read_namespaced_secret.return_value = secret

        nodes = MagicMock()
        nodes.items = []
        mock_api_client = MagicMock()
        mock_k8s_config.new_client_from_config_dict.return_value = mock_api_client
        mock_k8s_client.CoreV1Api.return_value.list_node.return_value = nodes

        result = discovery.discover_gpus("cluster-1", "kubeconfig-cluster-1", "psap-secrets")
        assert result.total_gpus == 0

    def test_invalid_kubeconfig_yaml(self) -> None:
        import base64 as b64

        discovery, mock_core = self._make_client()
        secret = MagicMock()
        secret.data = {"kubeconfig": b64.b64encode(b"just-a-plain-string").decode()}
        secret.string_data = None
        mock_core.read_namespaced_secret.return_value = secret

        with pytest.raises(GPUDiscoveryError, match="not valid YAML"):
            discovery.discover_gpus("cluster-1", "kubeconfig-cluster-1", "psap-secrets")

    @patch("fournos_cluster.core.gpu_discovery.k8s_config")
    @patch("fournos_cluster.core.gpu_discovery.client")
    def test_target_cluster_unreachable(
        self, mock_k8s_client: MagicMock, mock_k8s_config: MagicMock
    ) -> None:
        discovery, mock_core = self._make_client()
        self._mock_kubeconfig_secret(mock_core)

        mock_api_client = MagicMock()
        mock_k8s_config.new_client_from_config_dict.return_value = mock_api_client
        mock_k8s_client.CoreV1Api.return_value.list_node.side_effect = Exception(
            "Connection refused"
        )

        with pytest.raises(GPUDiscoveryError, match="Failed to connect"):
            discovery.discover_gpus("cluster-1", "kubeconfig-cluster-1", "psap-secrets")

        mock_api_client.close.assert_called_once()

    @patch("fournos_cluster.core.gpu_discovery.k8s_config")
    @patch("fournos_cluster.core.gpu_discovery.client")
    def test_auto_sets_current_context(
        self, mock_k8s_client: MagicMock, mock_k8s_config: MagicMock
    ) -> None:
        discovery, mock_core = self._make_client()

        kubeconfig_yaml = (
            "apiVersion: v1\nkind: Config\n"
            "contexts:\n- name: my-ctx\n  context:\n    cluster: c1\n"
            "clusters: []\nusers: []\n"
        )
        import base64
        secret = MagicMock()
        secret.data = {"kubeconfig": base64.b64encode(kubeconfig_yaml.encode()).decode()}
        secret.string_data = None
        mock_core.read_namespaced_secret.return_value = secret

        nodes = MagicMock()
        nodes.items = []
        mock_api_client = MagicMock()
        mock_k8s_config.new_client_from_config_dict.return_value = mock_api_client
        mock_k8s_client.CoreV1Api.return_value.list_node.return_value = nodes

        discovery.discover_gpus("cluster-1", "kubeconfig-cluster-1", "psap-secrets")

        config_dict = mock_k8s_config.new_client_from_config_dict.call_args[0][0]
        assert config_dict["current-context"] == "my-ctx"

    def test_no_contexts_raises(self) -> None:
        discovery, mock_core = self._make_client()

        import base64
        kubeconfig_yaml = "apiVersion: v1\nkind: Config\ncontexts: []\nclusters: []\nusers: []\n"
        secret = MagicMock()
        secret.data = {"kubeconfig": base64.b64encode(kubeconfig_yaml.encode()).decode()}
        secret.string_data = None
        mock_core.read_namespaced_secret.return_value = secret

        with pytest.raises(GPUDiscoveryError, match="has no contexts"):
            discovery.discover_gpus("cluster-1", "kubeconfig-cluster-1", "psap-secrets")

    @patch("fournos_cluster.core.gpu_discovery.k8s_config")
    @patch("fournos_cluster.core.gpu_discovery.client")
    def test_sets_request_timeout(
        self, mock_k8s_client: MagicMock, mock_k8s_config: MagicMock
    ) -> None:
        discovery, mock_core = self._make_client()
        self._mock_kubeconfig_secret(mock_core)

        nodes = MagicMock()
        nodes.items = []
        mock_api_client = MagicMock()
        mock_k8s_config.new_client_from_config_dict.return_value = mock_api_client
        mock_k8s_client.CoreV1Api.return_value.list_node.return_value = nodes

        discovery.discover_gpus("cluster-1", "kubeconfig-cluster-1", "psap-secrets")

        assert mock_api_client.configuration.request_timeout is not None


class TestDiscoveredGPU:

    def test_frozen(self) -> None:
        gpu = DiscoveredGPU(vendor="nvidia", model="A100", short_name="a100", count=8, node_count=2)
        with pytest.raises(AttributeError):
            gpu.count = 16

    def test_fields(self) -> None:
        gpu = DiscoveredGPU(vendor="amd", model="MI300X", short_name="mi300x", count=4, node_count=1)
        assert gpu.vendor == "amd"
        assert gpu.short_name == "mi300x"
        assert gpu.count == 4


class TestDiscoveryResult:

    def test_frozen(self) -> None:
        result = DiscoveryResult(gpus=(), total_gpus=0, timestamp="2026-04-29T00:00:00Z")
        with pytest.raises(AttributeError):
            result.total_gpus = 10
