"""Overrides for the parent integration-style conftest.

The top-level ``tests/conftest.py`` targets a live kind cluster (session-scoped
``k8s`` fixture, autouse cleanup). Tests under ``tests/unit/`` are pure
mock-based unit tests and must not require real cluster access, so we shadow
those fixtures here with no-ops.
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="session")
def k8s():
    return None


@pytest.fixture(autouse=True)
def _clean_around_test():
    yield
