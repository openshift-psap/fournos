from __future__ import annotations

import os
import subprocess
import time

import httpx
import pytest

FOURNOS_URL = os.environ.get("FOURNOS_URL", "http://localhost:8000")
NAMESPACE = "psap-automation"


def _kubectl_delete_all(resource: str) -> None:
    subprocess.run(
        [
            "kubectl",
            "delete",
            resource,
            "-n",
            NAMESPACE,
            "-l",
            "app.kubernetes.io/managed-by=fournos",
            "--ignore-not-found",
        ],
        check=True,
        capture_output=True,
    )


@pytest.fixture(autouse=True)
def _clean_before_test():
    """Wipe Fournos resources before every test for a deterministic state."""
    _kubectl_delete_all("pipelineruns")
    _kubectl_delete_all("workloads")


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
    raise_on_timeout: bool = True,
) -> str:
    """Poll GET /api/v1/job/{id} until the status reaches one of *terminal* states.

    By default, raises ``AssertionError`` if the timeout expires before a
    terminal status is reached.  Pass ``raise_on_timeout=False`` to return
    the last observed status instead (useful for negative tests).
    """
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
    if raise_on_timeout:
        raise AssertionError(
            f"Job {job_id} did not reach {terminal} within {timeout}s "
            f"(last status: {status})"
        )
    return status


def workload_exists(job_id: str) -> bool:
    """Check whether a Kueue Workload for *job_id* exists via kubectl."""
    result = subprocess.run(
        ["kubectl", "get", "workload", f"fournos-{job_id}", "-n", "psap-automation"],
        capture_output=True,
    )
    return result.returncode == 0


def delete_pipelinerun(job_id: str) -> None:
    """Delete a PipelineRun for *job_id* via kubectl."""
    subprocess.run(
        [
            "kubectl",
            "delete",
            "pipelinerun",
            f"fournos-{job_id}",
            "-n",
            "psap-automation",
            "--ignore-not-found",
        ],
        check=True,
        capture_output=True,
    )


def complete_job(client: httpx.Client, job_id: str) -> None:
    """Call the completion callback for *job_id*."""
    resp = client.post(f"/api/v1/job/{job_id}/complete")
    assert resp.status_code == 204
