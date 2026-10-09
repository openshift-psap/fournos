"""Unit tests for the unreachable-cluster redirect logic in reconcile_pending.

Pure mock-based tests (no live cluster) covering:
- `_redirect_to_equivalent_cluster` eligibility rules
- `reconcile_pending` wiring the redirect in before the normal pending path
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from fournos.handlers.lifecycle import (
    _redirect_to_equivalent_cluster,
    reconcile_pending,
)


def _make_patch() -> MagicMock:
    p = MagicMock()
    p.status = {}
    p.spec = {}
    return p


def _body(name: str = "job-1") -> dict:
    return {"metadata": {"name": name, "uid": "uid-123"}}


class _PatchBase:
    @pytest.fixture(autouse=True)
    def _setup_ctx(self):
        self.mock_kueue = MagicMock()
        patcher = patch("fournos.handlers.lifecycle.ctx")
        self.mock_ctx = patcher.start()
        self.mock_ctx.kueue = self.mock_kueue
        yield
        patcher.stop()


class TestRedirectEligibility(_PatchBase):
    def test_no_cluster_set_no_redirect(self) -> None:
        spec = {"hardware": {"gpuType": "a100"}, "exclusive": True}
        status: dict = {}
        patch_obj = _make_patch()

        result = _redirect_to_equivalent_cluster(
            spec, "job-1", status, patch_obj, _body()
        )

        assert result is False
        self.mock_kueue.delete_workload.assert_not_called()

    def test_no_gpu_type_no_redirect(self) -> None:
        """Lock-only jobs (cluster pinned, no hardware) have no equivalent to
        redirect to, since the user explicitly wanted that one cluster."""
        spec = {"cluster": "cluster-1", "exclusive": True}
        status: dict = {}
        patch_obj = _make_patch()

        result = _redirect_to_equivalent_cluster(
            spec, "job-1", status, patch_obj, _body()
        )

        assert result is False
        self.mock_kueue.delete_workload.assert_not_called()

    def test_healthy_cluster_no_redirect(self) -> None:
        spec = {
            "cluster": "cluster-1",
            "hardware": {"gpuType": "a100"},
            "exclusive": True,
        }
        status: dict = {}
        patch_obj = _make_patch()
        self.mock_kueue.is_flavor_healthy.return_value = True

        result = _redirect_to_equivalent_cluster(
            spec, "job-1", status, patch_obj, _body()
        )

        assert result is False
        self.mock_kueue.delete_workload.assert_not_called()

    def test_already_redirected_from_same_cluster_no_repeat(self) -> None:
        spec = {
            "cluster": "cluster-1",
            "hardware": {"gpuType": "a100"},
            "exclusive": True,
        }
        status = {"redirectedFrom": "cluster-1"}
        patch_obj = _make_patch()
        self.mock_kueue.is_flavor_healthy.return_value = False

        result = _redirect_to_equivalent_cluster(
            spec, "job-1", status, patch_obj, _body()
        )

        assert result is False
        self.mock_kueue.delete_workload.assert_not_called()

    def test_unhealthy_cluster_triggers_redirect(self) -> None:
        spec = {
            "cluster": "cluster-1",
            "hardware": {"gpuType": "a100", "gpuCount": 8},
            "exclusive": True,
            "priority": "nightly",
        }
        status: dict = {}
        patch_obj = _make_patch()
        self.mock_kueue.is_flavor_healthy.return_value = False

        result = _redirect_to_equivalent_cluster(
            spec, "job-1", status, patch_obj, _body()
        )

        assert result is True
        self.mock_kueue.delete_workload.assert_called_once_with("job-1")
        self.mock_kueue.create_workload.assert_called_once_with(
            name="job-1",
            gpu_type="a100",
            gpu_count=8,
            cluster=None,
            exclusive=True,
            priority="nightly",
            owner_ref={
                "apiVersion": "fournos.dev/v1",
                "kind": "FournosJob",
                "name": "job-1",
                "uid": "uid-123",
                "controller": True,
                "blockOwnerDeletion": True,
            },
        )
        assert patch_obj.status["redirectedFrom"] == "cluster-1"
        assert patch_obj.status["redirectCount"] == 1

    def test_redirect_count_increments_on_existing_value(self) -> None:
        spec = {
            "cluster": "cluster-2",
            "hardware": {"gpuType": "h100"},
            "exclusive": False,
        }
        status = {"redirectedFrom": "cluster-1", "redirectCount": 2}
        patch_obj = _make_patch()
        self.mock_kueue.is_flavor_healthy.return_value = False

        result = _redirect_to_equivalent_cluster(
            spec, "job-1", status, patch_obj, _body()
        )

        assert result is True
        assert patch_obj.status["redirectedFrom"] == "cluster-2"
        assert patch_obj.status["redirectCount"] == 3

    def test_non_exclusive_cluster_pinned_job_also_eligible(self) -> None:
        """A shared-access (exclusive: false) cluster+hardware pin has the
        same dead-end problem and should still be eligible for redirect."""
        spec = {
            "cluster": "cluster-1",
            "hardware": {"gpuType": "a100"},
            "exclusive": False,
        }
        status: dict = {}
        patch_obj = _make_patch()
        self.mock_kueue.is_flavor_healthy.return_value = False

        result = _redirect_to_equivalent_cluster(
            spec, "job-1", status, patch_obj, _body()
        )

        assert result is True
        self.mock_kueue.create_workload.assert_called_once()
        assert self.mock_kueue.create_workload.call_args.kwargs["exclusive"] is False


class TestReconcilePendingRedirectWiring(_PatchBase):
    def _not_admitted_workload(self) -> dict:
        return {"status": {"conditions": []}}

    def test_redirect_short_circuits_before_pending_message(self) -> None:
        spec = {
            "cluster": "cluster-1",
            "hardware": {"gpuType": "a100", "gpuCount": 8},
            "exclusive": True,
        }
        status: dict = {}
        patch_obj = _make_patch()

        self.mock_kueue.get_workload_or_none.return_value = (
            self._not_admitted_workload()
        )
        self.mock_kueue.is_flavor_healthy.return_value = False

        with patch(
            "fournos.handlers.lifecycle.KueueClient.is_admitted", return_value=False
        ):
            reconcile_pending(spec, "job-1", status, patch_obj, _body())

        self.mock_kueue.delete_workload.assert_called_once_with("job-1")
        self.mock_kueue.create_workload.assert_called_once()
        self.mock_kueue.get_pending_message.assert_not_called()

    def test_healthy_cluster_falls_through_to_normal_pending_path(self) -> None:
        spec = {
            "cluster": "cluster-1",
            "hardware": {"gpuType": "a100", "gpuCount": 8},
            "exclusive": True,
        }
        status: dict = {}
        patch_obj = _make_patch()

        self.mock_kueue.get_workload_or_none.return_value = (
            self._not_admitted_workload()
        )
        self.mock_kueue.is_flavor_healthy.return_value = True
        self.mock_kueue.get_pending_message.return_value = ("", "")

        with patch(
            "fournos.handlers.lifecycle.KueueClient.is_admitted", return_value=False
        ):
            reconcile_pending(spec, "job-1", status, patch_obj, _body())

        self.mock_kueue.delete_workload.assert_not_called()
        self.mock_kueue.create_workload.assert_not_called()
