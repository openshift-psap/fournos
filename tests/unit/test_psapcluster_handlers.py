"""Tests for PSAPCluster handler logic."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from fournos.handlers.psapcluster import (
    _build_gpu_summary,
    _reconcile_gpu_discovery,
    _reconcile_ttl_expiry,
    _reconcile_cluster_queue,
    on_psapcluster_create,
    on_psapcluster_owner_change,
    parse_duration,
    reconcile_psapcluster,
)


class TestParseDuration:

    def test_minutes(self) -> None:
        assert parse_duration("30m") == timedelta(minutes=30)

    def test_hours(self) -> None:
        assert parse_duration("4h") == timedelta(hours=4)

    def test_days(self) -> None:
        assert parse_duration("2d") == timedelta(days=2)

    def test_single_digit(self) -> None:
        assert parse_duration("1h") == timedelta(hours=1)

    def test_large_value(self) -> None:
        assert parse_duration("120m") == timedelta(minutes=120)

    def test_invalid_format(self) -> None:
        with pytest.raises(ValueError, match="Invalid duration"):
            parse_duration("abc")

    def test_invalid_unit(self) -> None:
        with pytest.raises(ValueError, match="Invalid duration"):
            parse_duration("10s")

    def test_empty(self) -> None:
        with pytest.raises(ValueError, match="Invalid duration"):
            parse_duration("")


class TestBuildGPUSummary:

    def test_single_gpu(self) -> None:
        gpus = [{"shortName": "a100", "count": 8}]
        assert _build_gpu_summary(gpus) == "8x A100"

    def test_multiple_gpus(self) -> None:
        gpus = [
            {"shortName": "a100", "count": 8},
            {"shortName": "h200", "count": 4},
        ]
        assert _build_gpu_summary(gpus) == "8x A100, 4x H200"

    def test_empty(self) -> None:
        assert _build_gpu_summary([]) == ""


class _PatchBase:
    """Mixin providing a patched ctx for handler tests."""

    @pytest.fixture(autouse=True)
    def _setup_ctx(self) -> None:
        self.mock_registry = MagicMock()
        self.mock_psapcluster = MagicMock()
        self.mock_gpu_discovery = MagicMock()

        patcher_registry = patch("fournos.handlers.psapcluster.ctx")
        self.mock_ctx = patcher_registry.start()
        self.mock_ctx.registry = self.mock_registry
        self.mock_ctx.psapcluster = self.mock_psapcluster
        self.mock_ctx.gpu_discovery = self.mock_gpu_discovery
        self.mock_psapcluster.cluster_queue_name.return_value = "fournos-test-cluster"

        yield
        patcher_registry.stop()

    def _make_patch(self) -> MagicMock:
        p = MagicMock()
        p.status = {}
        p.spec = {}
        return p


class TestOnCreate(_PatchBase):

    @patch("fournos.handlers.psapcluster._check_kubeconfig", return_value="Valid")
    def test_initializes_status(self, mock_check: MagicMock) -> None:
        patch_obj = self._make_patch()
        spec = {"kubeconfigSecret": "kubeconfig-cluster-1"}

        on_psapcluster_create(spec, "cluster-1", "psap-automation", {}, patch_obj, {})

        assert patch_obj.status["kubeconfigStatus"] == "Valid"
        assert patch_obj.status["locked"] is False
        assert patch_obj.status["clusterQueueName"] == "fournos-test-cluster"
        assert patch_obj.status["clusterQueueStatus"] == "Active"
        assert patch_obj.status["resourceFlavorName"] == "cluster-1"

        self.mock_psapcluster.ensure_resource_flavor.assert_called_once_with("cluster-1")
        self.mock_psapcluster.ensure_cluster_queue.assert_called_once_with("cluster-1")
        self.mock_psapcluster.ensure_local_queue.assert_called()

    @patch("fournos.handlers.psapcluster._check_kubeconfig", return_value="Missing")
    def test_missing_kubeconfig(self, mock_check: MagicMock) -> None:
        patch_obj = self._make_patch()
        spec = {"kubeconfigSecret": "kubeconfig-missing"}

        on_psapcluster_create(spec, "cluster-x", "psap-automation", {}, patch_obj, {})

        assert patch_obj.status["kubeconfigStatus"] == "Missing"

        conditions = patch_obj.status["conditions"]
        kubeconfig_cond = next(c for c in conditions if c["type"] == "KubeconfigValid")
        assert kubeconfig_cond["status"] == "False"

    @patch("fournos.handlers.psapcluster._check_kubeconfig", return_value="Valid")
    @patch("fournos.handlers.psapcluster._apply_lock")
    def test_owner_at_creation(self, mock_lock: MagicMock, mock_check: MagicMock) -> None:
        patch_obj = self._make_patch()
        spec = {"kubeconfigSecret": "kubeconfig-cluster-1", "owner": "nathan"}

        on_psapcluster_create(spec, "cluster-1", "psap-automation", {}, patch_obj, {})

        mock_lock.assert_called_once_with(spec, "cluster-1", patch_obj, "nathan")

    @patch("fournos.handlers.psapcluster._check_kubeconfig", return_value="Valid")
    def test_conditions_set(self, mock_check: MagicMock) -> None:
        patch_obj = self._make_patch()
        spec = {"kubeconfigSecret": "kubeconfig-cluster-1"}

        on_psapcluster_create(spec, "cluster-1", "psap-automation", {}, patch_obj, {})

        conditions = patch_obj.status["conditions"]
        types = {c["type"] for c in conditions}
        assert types == {"KubeconfigValid", "GPUDiscovered", "ClusterQueueReady"}


class TestOwnerChange(_PatchBase):

    def test_lock(self) -> None:
        patch_obj = self._make_patch()
        spec = {"kubeconfigSecret": "kc", "ttl": "4h"}

        on_psapcluster_owner_change(
            spec, "cluster-1", "psap-automation", {}, patch_obj, {}, old="", new="nathan"
        )

        self.mock_psapcluster.set_cluster_queue_stop_policy.assert_called_once_with(
            "cluster-1", "Hold"
        )
        assert patch_obj.status["locked"] is True
        assert patch_obj.status["clusterQueueStatus"] == "Held"
        assert patch_obj.status["lockExpiresAt"] is not None

    def test_lock_with_evict(self) -> None:
        patch_obj = self._make_patch()
        spec = {"kubeconfigSecret": "kc", "evict": True}

        on_psapcluster_owner_change(
            spec, "cluster-1", "psap-automation", {}, patch_obj, {}, old="", new="nathan"
        )

        self.mock_psapcluster.set_cluster_queue_stop_policy.assert_called_once_with(
            "cluster-1", "HoldAndDrain"
        )
        assert patch_obj.status["clusterQueueStatus"] == "HeldAndDraining"

    def test_lock_without_ttl(self) -> None:
        patch_obj = self._make_patch()
        spec = {"kubeconfigSecret": "kc"}

        on_psapcluster_owner_change(
            spec, "cluster-1", "psap-automation", {}, patch_obj, {}, old="", new="nathan"
        )

        assert patch_obj.status["locked"] is True
        assert patch_obj.status["lockExpiresAt"] is None

    def test_unlock(self) -> None:
        patch_obj = self._make_patch()
        spec = {"kubeconfigSecret": "kc"}

        on_psapcluster_owner_change(
            spec, "cluster-1", "psap-automation", {}, patch_obj, {}, old="nathan", new=""
        )

        self.mock_psapcluster.set_cluster_queue_stop_policy.assert_called_once_with(
            "cluster-1", "None"
        )
        assert patch_obj.status["locked"] is False
        assert patch_obj.status["clusterQueueStatus"] == "Active"
        assert patch_obj.status["lockExpiresAt"] is None
        assert patch_obj.status["ownerSetAt"] is None

    def test_lock_invalid_ttl_no_expiry(self) -> None:
        patch_obj = self._make_patch()
        spec = {"kubeconfigSecret": "kc", "ttl": "invalid"}

        on_psapcluster_owner_change(
            spec, "cluster-1", "psap-automation", {}, patch_obj, {}, old="", new="nathan"
        )

        assert patch_obj.status["locked"] is True
        assert patch_obj.status["lockExpiresAt"] is None


class TestReconcileTTLExpiry(_PatchBase):

    def test_expired_lock(self) -> None:
        patch_obj = self._make_patch()
        expired = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        status = {"locked": True, "lockExpiresAt": expired}
        spec = {"kubeconfigSecret": "kc", "owner": "nathan"}

        _reconcile_ttl_expiry(spec, "cluster-1", status, patch_obj)

        assert patch_obj.spec == {"owner": ""}
        assert patch_obj.status["locked"] is False
        self.mock_psapcluster.set_cluster_queue_stop_policy.assert_called_once_with(
            "cluster-1", "None"
        )

    def test_not_expired(self) -> None:
        patch_obj = self._make_patch()
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        status = {"locked": True, "lockExpiresAt": future}
        spec = {"kubeconfigSecret": "kc", "owner": "nathan"}

        _reconcile_ttl_expiry(spec, "cluster-1", status, patch_obj)

        assert patch_obj.spec == {}
        self.mock_psapcluster.set_cluster_queue_stop_policy.assert_not_called()

    def test_not_locked(self) -> None:
        patch_obj = self._make_patch()
        status = {"locked": False}
        spec = {"kubeconfigSecret": "kc"}

        _reconcile_ttl_expiry(spec, "cluster-1", status, patch_obj)

        self.mock_psapcluster.set_cluster_queue_stop_policy.assert_not_called()

    def test_locked_no_ttl(self) -> None:
        patch_obj = self._make_patch()
        status = {"locked": True, "lockExpiresAt": None}
        spec = {"kubeconfigSecret": "kc", "owner": "nathan"}

        _reconcile_ttl_expiry(spec, "cluster-1", status, patch_obj)

        self.mock_psapcluster.set_cluster_queue_stop_policy.assert_not_called()


class TestReconcileGPUDiscovery(_PatchBase):

    def test_skips_when_kubeconfig_invalid(self) -> None:
        patch_obj = self._make_patch()
        status = {"kubeconfigStatus": "Missing"}
        spec = {"kubeconfigSecret": "kc"}

        _reconcile_gpu_discovery(spec, "cluster-1", status, patch_obj)

        self.mock_gpu_discovery.discover_gpus.assert_not_called()

    def test_runs_discovery(self) -> None:
        from fournos.core.gpu_discovery import DiscoveredGPU, DiscoveryResult

        patch_obj = self._make_patch()
        status = {"kubeconfigStatus": "Valid", "hardware": {}}
        spec = {"kubeconfigSecret": "kc-cluster-1", "gpuDiscoveryInterval": "5m"}

        result = DiscoveryResult(
            gpus=(DiscoveredGPU("nvidia", "A100", "a100", 8, 2),),
            total_gpus=8,
            timestamp="2026-04-29T10:00:00Z",
        )
        self.mock_gpu_discovery.discover_gpus.return_value = result

        _reconcile_gpu_discovery(spec, "cluster-1", status, patch_obj)

        self.mock_gpu_discovery.discover_gpus.assert_called_once_with(
            "cluster-1", "kc-cluster-1", "psap-secrets"
        )
        assert patch_obj.status["hardware"]["totalGPUs"] == 8
        assert patch_obj.status["gpuSummary"] == "8x A100"
        assert patch_obj.status["hardware"]["consecutiveFailures"] == 0

    def test_respects_interval(self) -> None:
        patch_obj = self._make_patch()
        recent = datetime.now(timezone.utc).isoformat()
        status = {
            "kubeconfigStatus": "Valid",
            "hardware": {"lastDiscovery": recent, "consecutiveFailures": 0},
        }
        spec = {"kubeconfigSecret": "kc", "gpuDiscoveryInterval": "5m"}

        _reconcile_gpu_discovery(spec, "cluster-1", status, patch_obj)

        self.mock_gpu_discovery.discover_gpus.assert_not_called()

    def test_failure_increments_counter(self) -> None:
        from fournos.core.gpu_discovery import GPUDiscoveryError

        patch_obj = self._make_patch()
        status = {
            "kubeconfigStatus": "Valid",
            "hardware": {"consecutiveFailures": 2},
        }
        spec = {"kubeconfigSecret": "kc", "gpuDiscoveryInterval": "5m"}

        self.mock_gpu_discovery.discover_gpus.side_effect = GPUDiscoveryError("timeout")

        _reconcile_gpu_discovery(spec, "cluster-1", status, patch_obj)

        assert patch_obj.status["hardware"]["consecutiveFailures"] == 3
        assert patch_obj.status["hardware"]["lastError"] == "timeout"

    def test_five_failures_sets_unreachable(self) -> None:
        from fournos.core.gpu_discovery import GPUDiscoveryError

        patch_obj = self._make_patch()
        status = {
            "kubeconfigStatus": "Valid",
            "hardware": {"consecutiveFailures": 4},
        }
        spec = {"kubeconfigSecret": "kc", "gpuDiscoveryInterval": "5m"}

        self.mock_gpu_discovery.discover_gpus.side_effect = GPUDiscoveryError("timeout")

        _reconcile_gpu_discovery(spec, "cluster-1", status, patch_obj)

        assert patch_obj.status["kubeconfigStatus"] == "Unreachable"
        assert patch_obj.status["hardware"]["consecutiveFailures"] == 5

    def test_updates_cq_quotas_on_change(self) -> None:
        from fournos.core.gpu_discovery import DiscoveredGPU, DiscoveryResult

        patch_obj = self._make_patch()
        status = {
            "kubeconfigStatus": "Valid",
            "hardware": {
                "gpus": [{"shortName": "a100", "count": 4}],
            },
        }
        spec = {"kubeconfigSecret": "kc", "gpuDiscoveryInterval": "5m"}

        result = DiscoveryResult(
            gpus=(DiscoveredGPU("nvidia", "A100", "a100", 8, 2),),
            total_gpus=8,
            timestamp="2026-04-29T10:00:00Z",
        )
        self.mock_gpu_discovery.discover_gpus.return_value = result

        _reconcile_gpu_discovery(spec, "cluster-1", status, patch_obj)

        self.mock_psapcluster.update_cluster_queue_quotas.assert_called_once_with(
            "cluster-1", [("a100", 8)]
        )

    def test_no_cq_update_when_unchanged(self) -> None:
        from fournos.core.gpu_discovery import DiscoveredGPU, DiscoveryResult

        patch_obj = self._make_patch()
        status = {
            "kubeconfigStatus": "Valid",
            "hardware": {
                "gpus": [{"shortName": "a100", "count": 8}],
            },
        }
        spec = {"kubeconfigSecret": "kc", "gpuDiscoveryInterval": "5m"}

        result = DiscoveryResult(
            gpus=(DiscoveredGPU("nvidia", "A100", "a100", 8, 2),),
            total_gpus=8,
            timestamp="2026-04-29T10:00:00Z",
        )
        self.mock_gpu_discovery.discover_gpus.return_value = result

        _reconcile_gpu_discovery(spec, "cluster-1", status, patch_obj)

        self.mock_psapcluster.update_cluster_queue_quotas.assert_not_called()


class TestReconcileClusterQueue(_PatchBase):

    def test_recreates_missing_cq(self) -> None:
        patch_obj = self._make_patch()
        self.mock_psapcluster.get_cluster_queue_or_none.return_value = None
        status = {"locked": False, "hardware": {}}

        _reconcile_cluster_queue({}, "cluster-1", status, patch_obj)

        self.mock_psapcluster.ensure_cluster_queue.assert_called_once()
        self.mock_psapcluster.ensure_local_queue.assert_called()
        assert patch_obj.status["clusterQueueStatus"] == "Active"

    def test_recreates_missing_cq_locked(self) -> None:
        patch_obj = self._make_patch()
        self.mock_psapcluster.get_cluster_queue_or_none.return_value = None
        status = {"locked": True, "hardware": {}}

        _reconcile_cluster_queue({}, "cluster-1", status, patch_obj)

        args = self.mock_psapcluster.ensure_cluster_queue.call_args
        assert args[0][2] == "Hold"
        assert patch_obj.status["clusterQueueStatus"] == "Held"

    def test_recreates_missing_rf(self) -> None:
        patch_obj = self._make_patch()
        self.mock_psapcluster.get_cluster_queue_or_none.return_value = {"metadata": {}}
        self.mock_psapcluster.get_resource_flavor_or_none.return_value = None
        status = {}

        _reconcile_cluster_queue({}, "cluster-1", status, patch_obj)

        self.mock_psapcluster.ensure_resource_flavor.assert_called_once_with("cluster-1")

    def test_no_action_when_healthy(self) -> None:
        patch_obj = self._make_patch()
        self.mock_psapcluster.get_cluster_queue_or_none.return_value = {"metadata": {}}
        self.mock_psapcluster.get_resource_flavor_or_none.return_value = {"metadata": {}}
        status = {}

        _reconcile_cluster_queue({}, "cluster-1", status, patch_obj)

        self.mock_psapcluster.ensure_cluster_queue.assert_not_called()
        self.mock_psapcluster.ensure_resource_flavor.assert_not_called()


class TestReconcileFull(_PatchBase):

    @patch("fournos.handlers.psapcluster._reconcile_cluster_queue")
    @patch("fournos.handlers.psapcluster._reconcile_gpu_discovery")
    @patch("fournos.handlers.psapcluster._reconcile_ttl_expiry")
    @patch("fournos.handlers.psapcluster._reconcile_kubeconfig")
    def test_calls_all_steps(
        self,
        mock_kc: MagicMock,
        mock_ttl: MagicMock,
        mock_gpu: MagicMock,
        mock_cq: MagicMock,
    ) -> None:
        reconcile_psapcluster({}, "cluster-1", "ns", {}, MagicMock(), {})

        mock_kc.assert_called_once()
        mock_ttl.assert_called_once()
        mock_gpu.assert_called_once()
        mock_cq.assert_called_once()

    @patch("fournos.handlers.psapcluster._reconcile_cluster_queue")
    @patch("fournos.handlers.psapcluster._reconcile_gpu_discovery")
    @patch("fournos.handlers.psapcluster._reconcile_ttl_expiry")
    @patch("fournos.handlers.psapcluster._reconcile_kubeconfig")
    def test_order_kubeconfig_before_ttl_before_gpu(
        self,
        mock_kc: MagicMock,
        mock_ttl: MagicMock,
        mock_gpu: MagicMock,
        mock_cq: MagicMock,
    ) -> None:
        call_order = []
        mock_kc.side_effect = lambda *a, **kw: call_order.append("kubeconfig")
        mock_ttl.side_effect = lambda *a, **kw: call_order.append("ttl")
        mock_gpu.side_effect = lambda *a, **kw: call_order.append("gpu")
        mock_cq.side_effect = lambda *a, **kw: call_order.append("cq")

        reconcile_psapcluster({}, "cluster-1", "ns", {}, MagicMock(), {})

        assert call_order == ["kubeconfig", "ttl", "gpu", "cq"]
