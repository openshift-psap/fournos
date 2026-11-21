"""Unit tests for the per-node GPU capacity admission check."""

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from kubernetes.client.exceptions import ApiException

from fournos.core import node_fit
from fournos.core.kueue import KueueClient
from fournos.handlers import resolving
from fournos.state import ctx


@pytest.fixture(autouse=True)
def _clean_around_test():
    """Override integration cleanup; these tests use fake clients."""


class TestParseFreeBlocks:
    def test_parses_valid_annotation(self) -> None:
        assert node_fit.parse_free_blocks('{"a100": [4, 4], "h200": [8]}') == {
            "a100": [4, 4],
            "h200": [8],
        }

    def test_missing_annotation_is_unknown(self) -> None:
        assert node_fit.parse_free_blocks(None) is None

    def test_malformed_json_is_unknown(self) -> None:
        assert node_fit.parse_free_blocks("{not json") is None

    def test_non_dict_payload_is_unknown(self) -> None:
        assert node_fit.parse_free_blocks("[4, 4]") is None

    def test_non_list_sizes_are_unknown(self) -> None:
        assert node_fit.parse_free_blocks('{"a100": 4}') is None


class TestReplicaShape:
    def test_uses_reported_shape(self) -> None:
        hardware = {
            "gpuType": "h200",
            "gpuCount": 4,
            "replicas": 3,
            "gpusPerReplica": 4,
        }
        assert node_fit.replica_shape(hardware) == (3, 4)

    def test_legacy_hardware_is_one_replica(self) -> None:
        assert node_fit.replica_shape({"gpuCount": 8}) == (1, 8)

    def test_zero_gpu_count_means_nothing_to_check(self) -> None:
        assert node_fit.replica_shape({}) == (1, 0)

    def test_zero_replicas_is_reported_as_zero(self) -> None:
        assert node_fit.replica_shape({"gpuCount": 8, "replicas": 0}) == (0, 8)


class TestFitsOnNodes:
    def test_splits_across_nodes_is_not_a_fit(self) -> None:
        assert node_fit.fits_on_nodes([4, 4], replicas=1, gpus_per_replica=8) is False

    def test_single_node_holds_the_request(self) -> None:
        assert node_fit.fits_on_nodes([4, 4, 8], replicas=1, gpus_per_replica=8) is True

    def test_one_node_can_host_several_replicas(self) -> None:
        assert node_fit.fits_on_nodes([8], replicas=2, gpus_per_replica=4) is True

    def test_replicas_need_separate_bins_when_they_do_not_share(self) -> None:
        assert node_fit.fits_on_nodes([4, 4], replicas=2, gpus_per_replica=4) is True

    def test_not_enough_replica_slots(self) -> None:
        assert node_fit.fits_on_nodes([8, 4], replicas=4, gpus_per_replica=4) is False

    def test_full_cluster_has_no_room(self) -> None:
        assert node_fit.fits_on_nodes([], replicas=1, gpus_per_replica=1) is False

    def test_empty_request_always_fits(self) -> None:
        assert node_fit.fits_on_nodes([], replicas=1, gpus_per_replica=0) is True

    def test_zero_replicas_always_fits(self) -> None:
        assert node_fit.fits_on_nodes([], replicas=0, gpus_per_replica=8) is True


class TestShapeMessage:
    def test_lists_free_gpus_per_node(self) -> None:
        message = node_fit.shape_message("h200", 3, 4, [4, 8])

        assert "3 replica(s) of 4 'h200' GPU(s)" in message
        assert "8, 4" in message


class FakeFlavorClient:
    def __init__(self, flavor: dict | Exception) -> None:
        self._flavor = flavor

    def get_cluster_custom_object(self, **kwargs):
        if isinstance(self._flavor, Exception):
            raise self._flavor
        return self._flavor


class TestGetFlavorFreeBlocks:
    def test_reads_annotation(self) -> None:
        flavor = {
            "metadata": {
                "name": "cluster-1",
                "annotations": {"fournos.dev/free-gpu-blocks": '{"a100": [4]}'},
            }
        }
        client = KueueClient(FakeFlavorClient(flavor))

        assert client.get_flavor_free_blocks("cluster-1") == {"a100": [4]}

    def test_missing_annotation_is_unknown(self) -> None:
        flavor = {"metadata": {"name": "cluster-1"}}
        client = KueueClient(FakeFlavorClient(flavor))

        assert client.get_flavor_free_blocks("cluster-1") is None

    def test_missing_flavor_is_unknown(self) -> None:
        client = KueueClient(FakeFlavorClient(ApiException(status=404)))

        assert client.get_flavor_free_blocks("cluster-1") is None

    def test_other_errors_propagate(self) -> None:
        client = KueueClient(FakeFlavorClient(ApiException(status=500)))

        with pytest.raises(ApiException):
            client.get_flavor_free_blocks("cluster-1")


def _patch(monkeypatch, blocks, workload=None):
    monkeypatch.setattr(
        ctx,
        "kueue",
        SimpleNamespace(
            get_flavor_free_blocks=lambda cluster: blocks,
            get_workload_or_none=lambda name: workload,
        ),
    )
    return SimpleNamespace(status={})


class TestFitsFreeNodes:
    def test_skips_jobs_without_a_pinned_cluster(self, monkeypatch) -> None:
        patch = _patch(monkeypatch, {"h200": []})

        assert resolving._fits_free_nodes({}, "job", [], patch, "h200", 8) is True

    def test_skips_jobs_that_let_kueue_pick_the_cluster(self, monkeypatch) -> None:
        patch = _patch(monkeypatch, {"h200": []})
        spec = {"gpuPool": "pool-a", "hardware": {"gpuCount": 8}}

        assert resolving._fits_free_nodes(spec, "job", [], patch, "h200", 8) is True

    def test_skips_when_a_workload_already_exists(self, monkeypatch) -> None:
        patch = _patch(
            monkeypatch, {"h200": []}, workload={"metadata": {"name": "job"}}
        )
        spec = {"cluster": "cluster-1", "hardware": {"gpuType": "h200", "gpuCount": 8}}

        # The Workload was created on a previous tick; Kueue, not this gate,
        # decides. Otherwise a shrinking cluster could strand the job.
        assert resolving._fits_free_nodes(spec, "job", [], patch, "h200", 8) is True
        assert "conditions" not in patch.status

    def test_skips_when_capacity_is_unknown(self, monkeypatch) -> None:
        patch = _patch(monkeypatch, None)
        spec = {"cluster": "cluster-1", "hardware": {"gpuCount": 8}}

        assert resolving._fits_free_nodes(spec, "job", [], patch, "h200", 8) is True
        condition = patch.status["conditions"][0]
        assert condition["type"] == "NodeCapacity"
        assert condition["status"] == "Unknown"
        assert condition["reason"] == "CapacityUnknown"

    def test_unknown_capacity_clears_a_previous_rejection(self, monkeypatch) -> None:
        patch = _patch(monkeypatch, None)
        spec = {"cluster": "cluster-1", "hardware": {"gpuCount": 8}}
        stale = {
            "type": "NodeCapacity",
            "status": "False",
            "reason": "InsufficientNodeCapacity",
            "message": "stale",
        }

        resolving._fits_free_nodes(spec, "job", [stale], patch, "h200", 8)

        assert patch.status["conditions"][0]["status"] == "Unknown"

    def test_rejects_request_split_across_nodes(self, monkeypatch) -> None:
        patch = _patch(monkeypatch, {"h200": [4, 4]})
        spec = {"cluster": "cluster-1", "hardware": {"gpuType": "h200", "gpuCount": 8}}

        assert resolving._fits_free_nodes(spec, "job", [], patch, "h200", 8) is False
        assert "Cannot place 1 replica(s) of 8 'h200' GPU(s)" in patch.status["message"]
        condition = patch.status["conditions"][0]
        assert condition["type"] == "NodeCapacity"
        assert condition["status"] == "False"
        assert condition["reason"] == "InsufficientNodeCapacity"

    def test_rejects_when_replicas_exceed_free_blocks(self, monkeypatch) -> None:
        patch = _patch(monkeypatch, {"h200": [4, 4]})
        spec = {
            "cluster": "cluster-1",
            "hardware": {
                "gpuType": "h200",
                "gpuCount": 4,
                "replicas": 3,
                "gpusPerReplica": 4,
            },
        }

        assert resolving._fits_free_nodes(spec, "job", [], patch, "h200", 4) is False

    def test_accepts_a_fitting_shape(self, monkeypatch) -> None:
        patch = _patch(monkeypatch, {"h200": [8, 4]})
        spec = {
            "cluster": "cluster-1",
            "hardware": {
                "gpuType": "h200",
                "gpuCount": 4,
                "replicas": 3,
                "gpusPerReplica": 4,
            },
        }

        assert resolving._fits_free_nodes(spec, "job", [], patch, "h200", 4) is True
        assert patch.status["conditions"][0]["status"] == "True"


class TestReconcileResolvingGate:
    def _stub_resolution(self, monkeypatch, blocks):
        monkeypatch.setattr(resolving, "_ensure_resolve_job", lambda *args: {})
        monkeypatch.setattr(resolving, "_check_job_finished", lambda *args: True)
        monkeypatch.setattr(resolving, "_resolve_hardware", lambda *args: ("h200", 8))
        monkeypatch.setattr(resolving, "_validate_secret_refs", lambda *args: True)
        monkeypatch.setattr(
            ctx,
            "kueue",
            SimpleNamespace(
                get_flavor_free_blocks=lambda cluster: blocks,
                get_workload_or_none=lambda name: None,
            ),
        )

    def test_does_not_create_workload_when_replicas_do_not_fit(
        self, monkeypatch
    ) -> None:
        self._stub_resolution(monkeypatch, {"h200": [4, 4]})
        created = []
        monkeypatch.setattr(
            resolving,
            "_create_workload_and_transition",
            lambda *args: created.append(args),
        )
        spec = {
            "exclusive": True,
            "cluster": "cluster-1",
            "hardware": {"gpuType": "h200", "gpuCount": 8},
        }
        patch = SimpleNamespace(status={})

        resolving.reconcile_resolving(spec, "job", {}, patch, {"metadata": {}})

        assert created == []
        assert "phase" not in patch.status
        assert patch.status["message"].startswith("Cannot place")

    def test_creates_workload_when_replicas_fit(self, monkeypatch) -> None:
        self._stub_resolution(monkeypatch, {"h200": [8]})
        created = []
        monkeypatch.setattr(
            resolving,
            "_create_workload_and_transition",
            lambda *args: created.append(args),
        )
        spec = {
            "exclusive": True,
            "cluster": "cluster-1",
            "hardware": {"gpuType": "h200", "gpuCount": 8},
        }
        patch = SimpleNamespace(status={})

        resolving.reconcile_resolving(spec, "job", {}, patch, {"metadata": {}})

        assert len(created) == 1


def test_crd_schema_keeps_the_replica_shape_fields() -> None:
    """Unknown fields under spec.hardware are pruned by the API server.

    Forge writes replicas/gpusPerReplica through the resolver Job; if the CRD
    does not declare them, Fournos never sees them and silently falls back to
    a single replica of gpuCount.
    """
    crd = yaml.safe_load(
        (Path(__file__).parents[1] / "manifests" / "crd.yaml").read_text()
    )
    schema = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
    hardware = schema["properties"]["spec"]["properties"]["hardware"]

    assert {"replicas", "gpusPerReplica"} <= set(hardware["properties"])
