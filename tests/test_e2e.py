"""End-to-end tests against a running Fournos instance.

Requires:
  - Fournos running (e.g. ``make dev-run``)
  - A kind cluster with mock resources (``make dev-setup``)

Set ``FOURNOS_URL`` env var to override the default ``http://localhost:8000``.
"""

from __future__ import annotations

import subprocess

import httpx
import pytest

from tests.conftest import poll_job_status, submit_job


class TestE2E:
    """Sequential e2e scenario that mirrors dev/test.sh."""

    job_a: str
    job_ro: str
    job_b: str
    job_c: str

    # -----------------------------------------------------------------
    # Health
    # -----------------------------------------------------------------

    def test_health(self, client: httpx.Client):
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    # -----------------------------------------------------------------
    # Mode A: explicit cluster
    # -----------------------------------------------------------------

    def test_mode_a_submit(self, client: httpx.Client):
        data = submit_job(
            client,
            {
                "name": "test-explicit",
                "cluster": "cluster-1",
                "forge": {"project": "testproj/llmd", "preset": "cks"},
            },
        )
        TestE2E.job_a = data["id"]
        assert data["status"] in ("running", "succeeded")
        assert data["pipeline_run"] is not None

    def test_mode_a_status(self, client: httpx.Client):
        resp = client.get(f"/api/v1/job/{self.job_a}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == self.job_a
        assert data["status"] in ("running", "succeeded")

    # -----------------------------------------------------------------
    # Run-only pipeline (explicit cluster)
    # -----------------------------------------------------------------

    def test_run_only_pipeline_submit(self, client: httpx.Client):
        data = submit_job(
            client,
            {
                "name": "test-run-only",
                "pipeline": "fournos-run-only",
                "cluster": "cluster-2",
                "forge": {"project": "testproj/llmd", "preset": "cks"},
            },
        )
        TestE2E.job_ro = data["id"]
        assert data["status"] in ("running", "succeeded")

    def test_run_only_pipeline_status(self, client: httpx.Client):
        resp = client.get(f"/api/v1/job/{self.job_ro}")
        assert resp.status_code == 200
        assert resp.json()["id"] == self.job_ro

    # -----------------------------------------------------------------
    # Mode B: hardware request (Kueue scheduling)
    # -----------------------------------------------------------------

    def test_mode_b_submit(self, client: httpx.Client):
        data = submit_job(
            client,
            {
                "name": "test-hardware",
                "hardware": {"gpu_type": "a100", "gpu_count": 2},
                "forge": {"project": "testproj/llmd", "preset": "llama3"},
                "priority": "nightly",
            },
        )
        TestE2E.job_b = data["id"]
        assert data["status"] == "pending"

    def test_mode_b_poll_until_launched(self, client: httpx.Client):
        status = poll_job_status(client, self.job_b, timeout=60)
        assert status in ("running", "succeeded", "failed"), (
            f"Expected terminal-ish state, still {status}"
        )

    # -----------------------------------------------------------------
    # Mode B: inadmissible workload (stays pending)
    # -----------------------------------------------------------------

    def test_inadmissible_submit(self, client: httpx.Client):
        data = submit_job(
            client,
            {
                "name": "test-inadmissible",
                "hardware": {"gpu_type": "a100", "gpu_count": 100},
                "forge": {"project": "testproj/llmd", "preset": "cks"},
            },
        )
        TestE2E.job_c = data["id"]
        assert data["status"] == "pending"

    def test_inadmissible_stays_pending(self, client: httpx.Client):
        status = poll_job_status(
            client,
            self.job_c,
            terminal={"running", "succeeded", "failed"},
            interval=3,
            timeout=9,
        )
        assert status == "pending"

    # -----------------------------------------------------------------
    # List jobs
    # -----------------------------------------------------------------

    def test_list_all_jobs(self, client: httpx.Client):
        resp = client.get("/api/v1/jobs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 4

    def test_list_jobs_filtered_pending(self, client: httpx.Client):
        resp = client.get("/api/v1/jobs", params={"status": "pending"})
        assert resp.status_code == 200
        data = resp.json()
        assert all(j["status"] == "pending" for j in data["jobs"])
        assert data["count"] >= 1

    def test_list_jobs_filtered_running(self, client: httpx.Client):
        resp = client.get("/api/v1/jobs", params={"status": "running"})
        assert resp.status_code == 200
        assert all(j["status"] == "running" for j in resp.json()["jobs"])

    # -----------------------------------------------------------------
    # Complete callback
    # -----------------------------------------------------------------

    def test_complete_callback(self, client: httpx.Client):
        resp = client.post(f"/api/v1/job/{self.job_a}/complete")
        assert resp.status_code == 204

    def test_complete_callback_idempotent(self, client: httpx.Client):
        resp = client.post(f"/api/v1/job/{self.job_a}/complete")
        assert resp.status_code == 204

    # -----------------------------------------------------------------
    # Artifacts (stub)
    # -----------------------------------------------------------------

    def test_artifacts(self, client: httpx.Client):
        resp = client.get(f"/api/v1/job/{self.job_a}/artifacts")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == self.job_a

    # -----------------------------------------------------------------
    # Verify in-cluster K8s resources (informational, never fails)
    # -----------------------------------------------------------------

    @pytest.mark.filterwarnings("ignore")
    def test_k8s_resources(self, capsys):
        for kind in ("pipelineruns", "workloads"):
            result = subprocess.run(
                ["kubectl", "get", kind, "-n", "psap-automation", "-o", "wide"],
                capture_output=True,
                text=True,
            )
            with capsys.disabled():
                header = f"--- {kind} in psap-automation ---"
                print(f"\n{header}")
                print(result.stdout or "(no output)")
                if result.returncode != 0:
                    print(f"(kubectl exit code {result.returncode})")
