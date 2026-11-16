"""Unit tests for the fournos CLI — no live cluster required.

The parent ``tests/conftest.py`` wires every test to a live cluster via an
autouse cleanup fixture; override it with a no-op so these tests run anywhere.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clean_around_test():
    yield
