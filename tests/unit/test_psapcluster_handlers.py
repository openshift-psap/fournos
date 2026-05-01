"""Tests for PSAPCluster handler logic."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch, call

import pytest
from kubernetes.client.exceptions import ApiException

from fournos.handlers.psapcluster import (
    _build_gpu_summary,
    _reconcile_gpu_discovery,
    _reconcile_lock_job,
    _reconcile_ttl_expiry,
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
    """Mixin providing a patched ctx and K8s client for handler tests."""

    @pytest.fixture(autouse=True)
    def _setup_ctx(self) -> None:
        self.mock_registry = MagicMock()
        self.mock_gpu_discovery = MagicMock()
        self.mock_kueue = MagicMock()

        patcher_ctx = patch("fournos.handlers.psapcluster.ctx")
        self.mock_ctx = patcher_ctx.start()
        self.mock_ctx.registry = self.mock_registry
        self.mock_ctx.gpu_discovery = self.mock_gpu_discovery
        self.mock_ctx.kueue = self.mock_kueue

        patcher_client = patch("fournos.handlers.psapcluster.client")
        self.mock_k8s_client = patcher_client.start()
        self.mock_custom_api = MagicMock()
        self.mock_k8s_client.CustomObjectsApi.return_value = self.mock_custom_api
        self.mock_k8s_client.exceptions.ApiException = ApiException

        yield
        patcher_ctx.stop()
        patcher_client.stop()

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
        assert types == {"KubeconfigValid", "GPUDiscovered"}


class TestOwnerChange(_PatchBase):

    def test_lock_creates_sentinel(self) -> None:
        patch_obj = self._make_patch()
        spec = {"kubeconfigSecret": "kc", "ttl": "4h"}

        on_psapcluster_owner_change(
            spec, "cluster-1", "psap-automation", {}, patch_obj, {}, old="", new="nathan"
        )

        assert patch_obj.status["locked"] is True
        assert patch_obj.status["lockJobName"] == "psapcluster-lock-cluster-1"
        assert patch_obj.status["lockExpiresAt"] is not None

        self.mock_custom_api.create_namespaced_custom_object.assert_called_once()
        body = self.mock_custom_api.create_namespaced_custom_object.call_args[1]["body"]
        assert body["metadata"]["name"] == "psapcluster-lock-cluster-1"
        assert body["spec"]["lockOnly"] is True
        assert body["spec"]["exclusive"] is True
        assert body["spec"]["cluster"] == "cluster-1"
        assert body["spec"]["owner"] == "nathan"

    def test_lock_without_ttl(self) -> None:
        patch_obj = self._make_patch()
        spec = {"kubeconfigSecret": "kc"}

        on_psapcluster_owner_change(
            spec, "cluster-1", "psap-automation", {}, patch_obj, {}, old="", new="nathan"
        )

        assert patch_obj.status["locked"] is True
        assert patch_obj.status["lockExpiresAt"] is None

    def test_unlock_deletes_sentinel(self) -> None:
        patch_obj = self._make_patch()
        spec = {"kubeconfigSecret": "kc"}

        on_psapcluster_owner_change(
            spec, "cluster-1", "psap-automation", {}, patch_obj, {}, old="nathan", new=""
        )

        self.mock_custom_api.delete_namespaced_custom_object.assert_called_once()
        delete_args = self.mock_custom_api.delete_namespaced_custom_object.call_args[1]
        assert delete_args["name"] == "psapcluster-lock-cluster-1"

        assert patch_obj.status["locked"] is False
        assert patch_obj.status["lockExpiresAt"] is None
        assert patch_obj.status["ownerSetAt"] is None
        assert patch_obj.status["lockJobName"] is None

    def test_lock_invalid_ttl_no_expiry(self) -> None:
        patch_obj = self._make_patch()
        spec = {"kubeconfigSecret": "kc", "ttl": "invalid"}

        on_psapcluster_owner_change(
            spec, "cluster-1", "psap-automation", {}, patch_obj, {}, old="", new="nathan"
        )

        assert patch_obj.status["locked"] is True
        assert patch_obj.status["lockExpiresAt"] is None

    def test_lock_sentinel_already_exists(self) -> None:
        patch_obj = self._make_patch()
        spec = {"kubeconfigSecret": "kc"}
        self.mock_custom_api.create_namespaced_custom_object.side_effect = ApiException(status=409)

        on_psapcluster_owner_change(
            spec, "cluster-1", "psap-automation", {}, patch_obj, {}, old="", new="nathan"
        )

        assert patch_obj.status["locked"] is True

    def test_unlock_sentinel_already_deleted(self) -> None:
        patch_obj = self._make_patch()
        spec = {"kubeconfigSecret": "kc"}
        self.mock_custom_api.delete_namespaced_custom_object.side_effect = ApiException(status=404)

        on_psapcluster_owner_change(
            spec, "cluster-1", "psap-automation", {}, patch_obj, {}, old="nathan", new=""
        )

        assert patch_obj.status["locked"] is False


class TestReconcileTTLExpiry(_PatchBase):

    def test_expired_lock(self) -> None:
        patch_obj = self._make_patch()
        expired = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        status = {"locked": True, "lockExpiresAt": expired}
        spec = {"kubeconfigSecret": "kc", "owner": "nathan"}

        _reconcile_ttl_expiry(spec, "cluster-1", status, patch_obj)

        assert patch_obj.spec == {"owner": ""}
        assert patch_obj.status["locked"] is False
        self.mock_custom_api.delete_namespaced_custom_object.assert_called_once()

    def test_not_expired(self) -> None:
        patch_obj = self._make_patch()
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        status = {"locked": True, "lockExpiresAt": future}
        spec = {"kubeconfigSecret": "kc", "owner": "nathan"}

        _reconcile_ttl_expiry(spec, "cluster-1", status, patch_obj)

        assert patch_obj.spec == {}
        self.mock_custom_api.delete_namespaced_custom_object.assert_not_called()

    def test_not_locked(self) -> None:
        patch_obj = self._make_patch()
        status = {"locked": False}
        spec = {"kubeconfigSecret": "kc"}

        _reconcile_ttl_expiry(spec, "cluster-1", status, patch_obj)

        self.mock_custom_api.delete_namespaced_custom_object.assert_not_called()

    def test_locked_no_ttl(self) -> None:
        patch_obj = self._make_patch()
        status = {"locked": True, "lockExpiresAt": None}
        spec = {"kubeconfigSecret": "kc", "owner": "nathan"}

        _reconcile_ttl_expiry(spec, "cluster-1", status, patch_obj)

        self.mock_custom_api.delete_namespaced_custom_object.assert_not_called()


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

    def test_updates_flavor_quotas_on_change(self) -> None:
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

        self.mock_kueue.update_flavor_quotas.assert_called_once_with(
            "cluster-1", [("a100", 8)]
        )

    def test_no_quota_update_when_unchanged(self) -> None:
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

        self.mock_kueue.update_flavor_quotas.assert_not_called()


class TestReconcileLockJob(_PatchBase):

    def test_skips_when_not_locked(self) -> None:
        patch_obj = self._make_patch()
        status = {"locked": False}

        _reconcile_lock_job({}, "cluster-1", status, patch_obj)

        self.mock_custom_api.get_namespaced_custom_object.assert_not_called()

    def test_no_action_when_sentinel_exists(self) -> None:
        patch_obj = self._make_patch()
        status = {"locked": True}
        spec = {"owner": "nathan"}
        self.mock_custom_api.get_namespaced_custom_object.return_value = {"metadata": {}}

        _reconcile_lock_job(spec, "cluster-1", status, patch_obj)

        self.mock_custom_api.create_namespaced_custom_object.assert_not_called()

    def test_recreates_missing_sentinel(self) -> None:
        patch_obj = self._make_patch()
        status = {"locked": True}
        spec = {"owner": "nathan"}
        self.mock_custom_api.get_namespaced_custom_object.side_effect = ApiException(status=404)

        _reconcile_lock_job(spec, "cluster-1", status, patch_obj)

        self.mock_custom_api.create_namespaced_custom_object.assert_called_once()
        body = self.mock_custom_api.create_namespaced_custom_object.call_args[1]["body"]
        assert body["spec"]["lockOnly"] is True
        assert body["spec"]["owner"] == "nathan"

    def test_clears_lock_when_sentinel_missing_and_no_owner(self) -> None:
        patch_obj = self._make_patch()
        status = {"locked": True}
        spec = {}
        self.mock_custom_api.get_namespaced_custom_object.side_effect = ApiException(status=404)

        _reconcile_lock_job(spec, "cluster-1", status, patch_obj)

        assert patch_obj.status["locked"] is False
        assert patch_obj.status["lockJobName"] is None


class TestReconcileFull(_PatchBase):

    @patch("fournos.handlers.psapcluster._reconcile_lock_job")
    @patch("fournos.handlers.psapcluster._reconcile_gpu_discovery")
    @patch("fournos.handlers.psapcluster._reconcile_ttl_expiry")
    @patch("fournos.handlers.psapcluster._reconcile_kubeconfig")
    def test_calls_all_steps(
        self,
        mock_kc: MagicMock,
        mock_ttl: MagicMock,
        mock_gpu: MagicMock,
        mock_lock: MagicMock,
    ) -> None:
        reconcile_psapcluster({}, "cluster-1", "ns", {}, MagicMock(), {})

        mock_kc.assert_called_once()
        mock_ttl.assert_called_once()
        mock_gpu.assert_called_once()
        mock_lock.assert_called_once()

    @patch("fournos.handlers.psapcluster._reconcile_lock_job")
    @patch("fournos.handlers.psapcluster._reconcile_gpu_discovery")
    @patch("fournos.handlers.psapcluster._reconcile_ttl_expiry")
    @patch("fournos.handlers.psapcluster._reconcile_kubeconfig")
    def test_order_kubeconfig_before_ttl_before_gpu(
        self,
        mock_kc: MagicMock,
        mock_ttl: MagicMock,
        mock_gpu: MagicMock,
        mock_lock: MagicMock,
    ) -> None:
        call_order = []
        mock_kc.side_effect = lambda *a, **kw: call_order.append("kubeconfig")
        mock_ttl.side_effect = lambda *a, **kw: call_order.append("ttl")
        mock_gpu.side_effect = lambda *a, **kw: call_order.append("gpu")
        mock_lock.side_effect = lambda *a, **kw: call_order.append("lock")

        reconcile_psapcluster({}, "cluster-1", "ns", {}, MagicMock(), {})

        assert call_order == ["kubeconfig", "ttl", "gpu", "lock"]
