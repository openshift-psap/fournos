"""Core API tests — health, explicit-cluster jobs, completion callback, artifacts."""

import httpx

from tests.conftest import complete_job, submit_job


# -----------------------------------------------------------------
# Health
# -----------------------------------------------------------------


def test_health(client: httpx.Client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# -----------------------------------------------------------------
# Mode A: explicit cluster — submit, status, run-only pipeline
# -----------------------------------------------------------------


def test_submit_and_status(client: httpx.Client):
    data = submit_job(
        client,
        {
            "name": "test-explicit",
            "cluster": "cluster-1",
            "forge": {"project": "testproj/llmd", "preset": "cks"},
        },
    )
    assert data["status"] in ("running", "succeeded")
    assert data["pipeline_run"] is not None

    resp = client.get(f"/api/v1/job/{data['id']}")
    assert resp.status_code == 200
    assert resp.json()["id"] == data["id"]
    assert resp.json()["status"] in ("running", "succeeded")

    complete_job(client, data["id"])


def test_run_only_pipeline(client: httpx.Client):
    data = submit_job(
        client,
        {
            "name": "test-run-only",
            "pipeline": "fournos-run-only",
            "cluster": "cluster-2",
            "forge": {"project": "testproj/llmd", "preset": "cks"},
        },
    )
    assert data["status"] in ("running", "succeeded")

    resp = client.get(f"/api/v1/job/{data['id']}")
    assert resp.status_code == 200
    assert resp.json()["id"] == data["id"]

    complete_job(client, data["id"])


def test_unknown_cluster(client: httpx.Client):
    resp = client.post(
        "/api/v1/jobs",
        json={
            "name": "test-unknown",
            "cluster": "no-such-cluster",
            "forge": {"project": "testproj/llmd", "preset": "cks"},
        },
    )
    assert resp.status_code == 404


# -----------------------------------------------------------------
# Completion callback
# -----------------------------------------------------------------


def test_complete_callback(client: httpx.Client):
    data = submit_job(
        client,
        {
            "name": "test-complete",
            "cluster": "cluster-1",
            "forge": {"project": "testproj/llmd", "preset": "cks"},
        },
    )
    resp = client.post(f"/api/v1/job/{data['id']}/complete")
    assert resp.status_code == 204


def test_complete_callback_idempotent(client: httpx.Client):
    data = submit_job(
        client,
        {
            "name": "test-complete-idem",
            "cluster": "cluster-1",
            "forge": {"project": "testproj/llmd", "preset": "cks"},
        },
    )
    resp1 = client.post(f"/api/v1/job/{data['id']}/complete")
    assert resp1.status_code == 204

    resp2 = client.post(f"/api/v1/job/{data['id']}/complete")
    assert resp2.status_code == 204


def test_complete_nonexistent_job(client: httpx.Client):
    resp = client.post("/api/v1/job/does-not-exist/complete")
    assert resp.status_code == 204


# -----------------------------------------------------------------
# Artifacts (stub)
# -----------------------------------------------------------------


def test_artifacts(client: httpx.Client):
    data = submit_job(
        client,
        {
            "name": "test-artifacts",
            "cluster": "cluster-1",
            "forge": {"project": "testproj/llmd", "preset": "cks"},
        },
    )

    resp = client.get(f"/api/v1/job/{data['id']}/artifacts")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == data["id"]
    assert isinstance(body["artifacts"], list)
