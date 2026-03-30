"""API tests — health, scheduling (cluster / hardware / both), validation,
completion callback, artifacts.
"""

import httpx

from tests.conftest import (
    complete_job,
    get_pipelinerun_param,
    get_workload_flavor,
    get_workload_node_selector,
    poll_job_status,
    submit_job,
)


# -----------------------------------------------------------------
# Health
# -----------------------------------------------------------------


def test_health(client: httpx.Client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# -----------------------------------------------------------------
# Scheduling — all jobs go through Kueue
# -----------------------------------------------------------------


def test_cluster_pinned(client: httpx.Client):
    """Submit with explicit cluster — Kueue pins to that flavor via nodeSelector."""
    data = submit_job(
        client,
        {
            "name": "test-cluster",
            "cluster": "cluster-2",
            "forge": {"project": "testproj/llmd", "preset": "cks"},
        },
    )
    job_id = data["id"]
    try:
        assert data["status"] == "pending"

        poll_job_status(client, job_id, timeout=30, terminal={"running"})

        resp = client.get(f"/api/v1/job/{job_id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == job_id
        assert resp.json()["cluster"] == "cluster-2"

        assert get_workload_node_selector(job_id) == {
            "fournos.dev/cluster": "cluster-2"
        }
        assert get_workload_flavor(job_id) == "cluster-2"
        assert (
            get_pipelinerun_param(job_id, "kubeconfig-secret") == "cluster-2-kubeconfig"
        )

        poll_job_status(
            client,
            job_id,
            terminal={"succeeded", "failed"},
            timeout=60,
        )
    finally:
        complete_job(client, job_id)


def test_hardware_request(client: httpx.Client):
    """Submit with hardware — Kueue picks a cluster with available quota."""
    data = submit_job(
        client,
        {
            "name": "test-hardware",
            "hardware": {"gpu_type": "a100", "gpu_count": 2},
            "forge": {"project": "testproj/llmd", "preset": "llama3"},
            "priority": "nightly",
        },
    )
    job_id = data["id"]
    try:
        assert data["status"] == "pending"

        poll_job_status(
            client,
            job_id,
            terminal={"succeeded", "failed"},
            timeout=60,
        )
    finally:
        complete_job(client, job_id)


def test_cluster_and_hardware(client: httpx.Client):
    """Submit with both cluster and hardware — pinned cluster with GPU quota."""
    data = submit_job(
        client,
        {
            "name": "test-cluster-hw",
            "cluster": "cluster-4",
            "hardware": {"gpu_type": "h200", "gpu_count": 2},
            "forge": {"project": "testproj/llmd", "preset": "llama3"},
        },
    )
    job_id = data["id"]
    try:
        assert data["status"] == "pending"

        poll_job_status(client, job_id, timeout=30, terminal={"running"})

        resp = client.get(f"/api/v1/job/{job_id}")
        assert resp.status_code == 200
        assert resp.json()["cluster"] == "cluster-4"

        assert get_workload_node_selector(job_id) == {
            "fournos.dev/cluster": "cluster-4"
        }
        assert get_workload_flavor(job_id) == "cluster-4"
        assert (
            get_pipelinerun_param(job_id, "kubeconfig-secret") == "cluster-4-kubeconfig"
        )

        poll_job_status(
            client,
            job_id,
            terminal={"succeeded", "failed"},
            timeout=60,
        )
    finally:
        complete_job(client, job_id)


def test_run_only_pipeline(client: httpx.Client):
    """Alternative pipeline selection works with cluster-pinned scheduling."""
    data = submit_job(
        client,
        {
            "name": "test-run-only",
            "pipeline": "fournos-run-only",
            "cluster": "cluster-2",
            "forge": {"project": "testproj/llmd", "preset": "cks"},
        },
    )
    job_id = data["id"]
    try:
        assert data["status"] == "pending"

        poll_job_status(
            client,
            job_id,
            terminal={"succeeded", "failed"},
            timeout=60,
        )
    finally:
        complete_job(client, job_id)


def test_inadmissible_stays_pending(client: httpx.Client):
    """Hardware request exceeding all quotas stays pending."""
    data = submit_job(
        client,
        {
            "name": "test-inadmissible",
            "hardware": {"gpu_type": "a100", "gpu_count": 100},
            "forge": {"project": "testproj/llmd", "preset": "cks"},
        },
    )
    job_id = data["id"]
    try:
        assert data["status"] == "pending"

        status = poll_job_status(
            client,
            job_id,
            terminal={"running", "succeeded", "failed"},
            interval=3,
            timeout=9,
            raise_on_timeout=False,
        )
        assert status == "pending"
    finally:
        complete_job(client, job_id)


def test_cluster_without_required_gpu_stays_pending(client: httpx.Client):
    """Requesting A100s on cluster-3 (which has 0 A100 quota) stays pending."""
    data = submit_job(
        client,
        {
            "name": "test-wrong-gpu",
            "cluster": "cluster-3",
            "hardware": {"gpu_type": "a100", "gpu_count": 2},
            "forge": {"project": "testproj/llmd", "preset": "cks"},
        },
    )
    job_id = data["id"]
    try:
        assert data["status"] == "pending"

        status = poll_job_status(
            client,
            job_id,
            terminal={"running", "succeeded", "failed"},
            interval=3,
            timeout=9,
            raise_on_timeout=False,
        )
        assert status == "pending"
    finally:
        complete_job(client, job_id)


# -----------------------------------------------------------------
# Validation
# -----------------------------------------------------------------


def test_validation_neither_cluster_nor_hardware(client: httpx.Client):
    resp = client.post(
        "/api/v1/jobs",
        json={
            "name": "test-invalid",
            "forge": {"project": "testproj/llmd", "preset": "cks"},
        },
    )
    assert resp.status_code == 400


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
    job_id = data["id"]
    try:
        poll_job_status(client, job_id, timeout=30)

        resp = client.get(f"/api/v1/job/{job_id}/artifacts")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == job_id
        assert isinstance(body["artifacts"], list)
    finally:
        complete_job(client, job_id)
