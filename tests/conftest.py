from __future__ import annotations

import json
import subprocess
import textwrap
import time
from typing import Any

import pytest
from kubernetes import client, config

from fournos.settings import settings

NAMESPACE = settings.namespace
GROUP = "fournos.dev"
VERSION = "v1"
PLURAL = "fournosjobs"


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


@pytest.fixture(scope="session")
def k8s():
    """Return a CustomObjectsApi client configured for the current context."""
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    return client.CustomObjectsApi()


@pytest.fixture(autouse=True)
def _clean_before_test(k8s):
    """Wipe all FournosJobs (and their child resources) for a deterministic state."""
    jobs = k8s.list_namespaced_custom_object(GROUP, VERSION, NAMESPACE, PLURAL)
    for job in jobs.get("items", []):
        name = job["metadata"]["name"]
        try:
            k8s.delete_namespaced_custom_object(
                GROUP,
                VERSION,
                NAMESPACE,
                PLURAL,
                name,
                grace_period_seconds=0,
            )
        except client.exceptions.ApiException as exc:
            if exc.status != 404:
                raise

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        remaining = k8s.list_namespaced_custom_object(GROUP, VERSION, NAMESPACE, PLURAL)
        if not remaining.get("items"):
            break
        time.sleep(1)

    _kubectl_delete_all("pipelineruns.tekton.dev")
    _kubectl_delete_all("workloads.kueue.x-k8s.io")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def create_job(k8s, name: str, spec: dict) -> dict:
    """Create a FournosJob CR and return the API response."""
    body = {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "FournosJob",
        "metadata": {"name": name, "namespace": NAMESPACE},
        "spec": spec,
    }
    return k8s.create_namespaced_custom_object(
        GROUP,
        VERSION,
        NAMESPACE,
        PLURAL,
        body,
    )


def get_job(k8s, name: str) -> dict:
    return k8s.get_namespaced_custom_object(GROUP, VERSION, NAMESPACE, PLURAL, name)


def get_job_phase(k8s, name: str) -> str:
    job = get_job(k8s, name)
    return job.get("status", {}).get("phase", "")


def poll_phase(
    k8s,
    name: str,
    *,
    terminal: set[str],
    message_substring: str | None = None,
    interval: float = 3.0,
    timeout: float = 60.0,
    raise_on_timeout: bool = True,
) -> str:
    """Poll the FournosJob until its phase reaches one of *terminal* states.

    When *message_substring* is provided, both the phase match **and** the
    substring match in ``status.message`` are required before returning.

    Raises ``AssertionError`` on timeout unless ``raise_on_timeout=False``,
    in which case the last observed phase is returned.
    """
    deadline = time.monotonic() + timeout
    phase = ""
    message = ""
    while True:
        job = get_job(k8s, name)
        phase = job.get("status", {}).get("phase", "")
        message = job.get("status", {}).get("message", "")
        phase_ok = phase in terminal
        message_ok = message_substring is None or message_substring in message
        if phase_ok and message_ok:
            return phase
        if time.monotonic() >= deadline:
            break
        time.sleep(interval)
    if raise_on_timeout:
        status = job.get("status", {})
        detail = f"phase={phase or '<unset>'}"
        if message:
            detail += f", message={message!r}"
        if message_substring and message_substring not in message:
            detail += f"\n  (expected message containing {message_substring!r})"
        for c in status.get("conditions", []):
            cond_str = f"{c['type']}={c['status']}"
            if c.get("reason"):
                cond_str += f" ({c['reason']})"
            if c.get("message"):
                cond_str += f": {c['message']}"
            detail += f"\n    {cond_str}"
        raise AssertionError(
            f"Job {name} did not reach {terminal} within {timeout}s.\n"
            f"  Current status: {detail}"
        )
    return phase


def job_status_summary(k8s, name: str) -> str:
    """Return a one-line status summary for use in assertion messages."""
    job = get_job(k8s, name)
    st = job.get("status", {})
    parts = [f"phase={st.get('phase', '<unset>')!r}"]
    if st.get("message"):
        parts.append(f"message={st['message']!r}")
    if st.get("cluster"):
        parts.append(f"cluster={st['cluster']!r}")
    for c in st.get("conditions", []):
        cond_str = f"{c['type']}={c['status']}"
        if c.get("reason"):
            cond_str += f"/{c['reason']}"
        parts.append(cond_str)
    return f"Job {name}: {', '.join(parts)}"


def workload_exists(name: str) -> bool:
    """Check whether the Kueue Workload *name* exists."""
    result = subprocess.run(
        ["kubectl", "get", "workload", name, "-n", NAMESPACE],
        capture_output=True,
    )
    return result.returncode == 0


def pipelinerun_exists(name: str) -> bool:
    """Check whether the Tekton PipelineRun *name* exists."""
    result = subprocess.run(
        ["kubectl", "get", "pipelinerun", name, "-n", NAMESPACE],
        capture_output=True,
    )
    return result.returncode == 0


def poll_resource_gone(
    check_fn,
    name: str,
    *,
    interval: float = 2.0,
    timeout: float = 30.0,
) -> None:
    """Poll until ``check_fn(name)`` returns False (resource deleted)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not check_fn(name):
            return
        time.sleep(interval)
    raise AssertionError(
        f"{check_fn.__name__}({name!r}): resource still exists after {timeout}s"
    )


def get_k8s_resource(kind: str, name: str) -> dict:
    """Fetch a namespaced K8s resource as a dict via kubectl."""
    result = subprocess.run(
        ["kubectl", "get", kind, name, "-n", NAMESPACE, "-o", "json"],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def get_workload_node_selector(name: str) -> dict:
    """Return the nodeSelector from the Workload's podSet template."""
    wl = get_k8s_resource("workload", name)
    pod_sets = wl.get("spec", {}).get("podSets", [])
    if not pod_sets:
        return {}
    return pod_sets[0].get("template", {}).get("spec", {}).get("nodeSelector", {})


def get_workload_cluster_slots(name: str) -> int:
    """Return the fournos/cluster-slot request from the Workload."""
    wl = get_k8s_resource("workload", name)
    pod_sets = wl.get("spec", {}).get("podSets", [])
    if not pod_sets:
        return 0
    containers = pod_sets[0].get("template", {}).get("spec", {}).get("containers", [])
    if not containers:
        return 0
    requests = containers[0].get("resources", {}).get("requests", {})
    return int(requests.get("fournos/cluster-slot", "0"))


def get_workload_flavor(name: str) -> str | None:
    """Return the ResourceFlavor Kueue assigned to the Workload."""
    wl = get_k8s_resource("workload", name)
    assignments = wl.get("status", {}).get("admission", {}).get("podSetAssignments", [])
    if not assignments:
        return None
    flavors = assignments[0].get("flavors", {})
    return next(iter(flavors.values()), None) if flavors else None


def get_pipelinerun_param(name: str, param_name: str) -> Any:
    """Return a named param value from the PipelineRun spec."""
    pr = get_k8s_resource("pipelinerun", name)
    for p in pr.get("spec", {}).get("params", []):
        if p["name"] == param_name:
            return p["value"]
    return None


def create_stale_workload(k8s, name: str) -> None:
    """Create a fournos-labeled Workload with no corresponding FournosJob."""
    body = {
        "apiVersion": "kueue.x-k8s.io/v1beta2",
        "kind": "Workload",
        "metadata": {
            "name": name,
            "namespace": NAMESPACE,
            "labels": {
                "app.kubernetes.io/managed-by": "fournos",
                "fournos.dev/job-name": name,
                "kueue.x-k8s.io/queue-name": "fournos-queue",
            },
        },
        "spec": {
            "queueName": "fournos-queue",
            "podSets": [
                {
                    "name": "launcher",
                    "count": 1,
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "placeholder",
                                    "image": "registry.k8s.io/pause:3.9",
                                    "resources": {
                                        "requests": {"fournos/cluster-slot": "1"}
                                    },
                                }
                            ],
                            "restartPolicy": "Never",
                        },
                    },
                }
            ],
        },
    }
    k8s.create_namespaced_custom_object(
        "kueue.x-k8s.io",
        "v1beta2",
        NAMESPACE,
        "workloads",
        body,
    )


def create_stale_pipelinerun(k8s, name: str) -> None:
    """Create a fournos-labeled PipelineRun with no corresponding FournosJob."""
    body = {
        "apiVersion": "tekton.dev/v1",
        "kind": "PipelineRun",
        "metadata": {
            "name": name,
            "namespace": NAMESPACE,
            "labels": {
                "app.kubernetes.io/managed-by": "fournos",
                "fournos.dev/job-name": name,
            },
        },
        "spec": {
            "pipelineRef": {"name": "fournos-run-only"},
            "params": [
                {"name": "job-name", "value": name},
                {"name": "forge-project", "value": "test/stale"},
                {
                    "name": "forge-config",
                    "value": textwrap.dedent("""\
                        project: test/stale
                        args:
                        - cks
                        - internal-test
                    """),
                },
                {"name": "env", "value": ""},
                {"name": "kubeconfig-secret", "value": "cluster-1-kubeconfig"},
                {"name": "gpu-count", "value": "0"},
            ],
        },
    }
    k8s.create_namespaced_custom_object(
        "tekton.dev",
        "v1",
        NAMESPACE,
        "pipelineruns",
        body,
    )
