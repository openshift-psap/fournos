"""Unit-test conftest — override the e2e autouse fixture so no K8s connection is needed."""

import pytest


@pytest.fixture(autouse=True)
def _clean_before_test():
    yield
