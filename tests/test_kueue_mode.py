"""Kueue mode: hardware request — scheduling, inadmissible workloads, validation."""

import httpx

from tests.conftest import complete_job, poll_job_status, submit_job


def test_hardware_request_admitted_and_launched(client: httpx.Client):
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

        poll_job_status(client, job_id, timeout=60)
        poll_job_status(
            client,
            job_id,
            terminal={"succeeded", "failed"},
            timeout=120,
        )
    finally:
        complete_job(client, job_id)


def test_inadmissible_stays_pending(client: httpx.Client):
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


def test_validation_both_cluster_and_hardware(client: httpx.Client):
    resp = client.post(
        "/api/v1/jobs",
        json={
            "name": "test-invalid",
            "cluster": "cluster-1",
            "hardware": {"gpu_type": "a100", "gpu_count": 1},
            "forge": {"project": "testproj/llmd", "preset": "cks"},
        },
    )
    assert resp.status_code == 400


def test_validation_neither_cluster_nor_hardware(client: httpx.Client):
    resp = client.post(
        "/api/v1/jobs",
        json={
            "name": "test-invalid",
            "forge": {"project": "testproj/llmd", "preset": "cks"},
        },
    )
    assert resp.status_code == 400
