"""Unit tests for KueueClient.is_flavor_healthy (no live cluster needed)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from kubernetes.client.exceptions import ApiException

from fournos.core.kueue import KueueClient


class TestIsFlavorHealthy:
    def test_healthy_when_label_true(self) -> None:
        mock_custom = MagicMock()
        mock_custom.get_cluster_custom_object.return_value = {
            "metadata": {"labels": {"fournos.dev/healthy": "true"}}
        }
        kueue = KueueClient(mock_custom)

        assert kueue.is_flavor_healthy("cluster-1") is True

    def test_unhealthy_when_label_false(self) -> None:
        mock_custom = MagicMock()
        mock_custom.get_cluster_custom_object.return_value = {
            "metadata": {"labels": {"fournos.dev/healthy": "false"}}
        }
        kueue = KueueClient(mock_custom)

        assert kueue.is_flavor_healthy("cluster-1") is False

    def test_defaults_healthy_when_unlabeled(self) -> None:
        mock_custom = MagicMock()
        mock_custom.get_cluster_custom_object.return_value = {
            "metadata": {"labels": {}}
        }
        kueue = KueueClient(mock_custom)

        assert kueue.is_flavor_healthy("cluster-1") is True

    def test_defaults_healthy_when_no_labels_key(self) -> None:
        mock_custom = MagicMock()
        mock_custom.get_cluster_custom_object.return_value = {"metadata": {}}
        kueue = KueueClient(mock_custom)

        assert kueue.is_flavor_healthy("cluster-1") is True

    def test_defaults_healthy_when_flavor_missing(self) -> None:
        mock_custom = MagicMock()
        mock_custom.get_cluster_custom_object.side_effect = ApiException(status=404)
        kueue = KueueClient(mock_custom)

        assert kueue.is_flavor_healthy("missing-cluster") is True

    def test_other_api_error_propagates(self) -> None:
        mock_custom = MagicMock()
        mock_custom.get_cluster_custom_object.side_effect = ApiException(status=500)
        kueue = KueueClient(mock_custom)

        with pytest.raises(ApiException):
            kueue.is_flavor_healthy("cluster-1")
