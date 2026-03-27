from __future__ import annotations

import os
import time

import httpx
import pytest

FOURNOS_URL = os.environ.get("FOURNOS_URL", "http://localhost:8000")


@pytest.fixture(scope="session")
def base_url() -> str:
    return FOURNOS_URL


@pytest.fixture(scope="session")
def client(base_url: str):
    with httpx.Client(base_url=base_url, timeout=30) as c:
        yield c


def submit_job(client: httpx.Client, payload: dict) -> dict:
    """POST /api/v1/jobs, assert 201, return the response body."""
    resp = client.post("/api/v1/jobs", json=payload)
    assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"
    return resp.json()


def poll_job_status(
    client: httpx.Client,
    job_id: str,
    *,
    terminal: set[str] = frozenset({"running", "succeeded", "failed"}),
    interval: float = 3.0,
    timeout: float = 60.0,
) -> str:
    """Poll GET /api/v1/job/{id} until the status reaches one of *terminal* states."""
    deadline = time.monotonic() + timeout
    status = None
    while True:
        resp = client.get(f"/api/v1/job/{job_id}")
        assert resp.status_code == 200
        status = resp.json()["status"]
        if status in terminal:
            return status
        if time.monotonic() >= deadline:
            break
        time.sleep(interval)
    return status
