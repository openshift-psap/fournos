"""GET /api/v1/jobs — list and filter jobs."""

import httpx

from tests.conftest import poll_job_status, submit_job


def test_list_all_jobs(client: httpx.Client):
    job_a = submit_job(
        client,
        {
            "name": "test-list-a",
            "cluster": "cluster-1",
            "forge": {"project": "testproj/llmd", "preset": "cks"},
        },
    )
    job_b = submit_job(
        client,
        {
            "name": "test-list-b",
            "cluster": "cluster-2",
            "forge": {"project": "testproj/llmd", "preset": "cks"},
        },
    )

    resp = client.get("/api/v1/jobs")
    assert resp.status_code == 200
    data = resp.json()
    ids = {j["id"] for j in data["jobs"]}
    assert job_a["id"] in ids
    assert job_b["id"] in ids
    assert data["count"] == 2


def test_filter_by_pending(client: httpx.Client):
    submit_job(
        client,
        {
            "name": "test-filter-pending",
            "hardware": {"gpu_type": "a100", "gpu_count": 100},
            "forge": {"project": "testproj/llmd", "preset": "cks"},
        },
    )

    resp = client.get("/api/v1/jobs", params={"status": "pending"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    assert all(j["status"] == "pending" for j in data["jobs"])


def test_filter_by_running(client: httpx.Client):
    data = submit_job(
        client,
        {
            "name": "test-filter-running",
            "cluster": "cluster-1",
            "forge": {"project": "testproj/llmd", "preset": "cks"},
        },
    )
    poll_job_status(
        client,
        data["id"],
        terminal={"running", "succeeded", "failed"},
        timeout=15,
    )

    resp = client.get("/api/v1/jobs", params={"status": "running"})
    assert resp.status_code == 200
    assert all(j["status"] == "running" for j in resp.json()["jobs"])
