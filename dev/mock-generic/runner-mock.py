#!/usr/bin/env python3
"""Mock fournos-generic runner for local kind dev clusters.

Same logic as config/generic/runner.py but uses kubectl instead of oc,
so it works on kind clusters without OpenShift.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time

FJOB_NAME = os.environ["FJOB_NAME"]
NAMESPACE = os.environ["FOURNOS_WORKLOAD_NAMESPACE"]
STEP = os.environ.get("FOURNOS_STEP", "run")
ARTIFACT_DIR = os.environ.get("ARTIFACT_DIR", "/tmp/artifacts")

POLL_INTERVAL = 5
JOB_TIMEOUT = int(os.environ.get("FOURNOS_GENERIC_JOB_TIMEOUT", "86400"))


def log(msg: str) -> None:
    print(f"[fournos-generic-mock] {msg}", flush=True)


def sh(*args: str, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, input=input_text, text=True, capture_output=True, check=check)


def get_fjob() -> dict:
    result = sh("kubectl", "get", f"fjob/{FJOB_NAME}", "-n", NAMESPACE, "-o", "json")
    fjob = json.loads(result.stdout)
    os.makedirs(ARTIFACT_DIR, exist_ok=True)
    with open(os.path.join(ARTIFACT_DIR, "fournos_fjob.json"), "w") as f:
        json.dump(fjob, f, indent=2)
    return fjob


def get_generic_config(fjob: dict) -> dict:
    spec = fjob.get("spec", {})
    cfg = (spec.get("executionEngine") or {}).get("generic")
    if cfg is None:
        log("ERROR: spec.executionEngine.generic is required")
        sys.exit(1)
    return cfg


def resolve(cfg: dict) -> None:
    image = cfg.get("image")
    if not image:
        log("ERROR: spec.executionEngine.generic.image is required")
        sys.exit(1)
    log(f"Resolved generic job for image={image!r}")


def _child_job_name(fjob_name: str) -> str:
    suffix = hashlib.sha256(fjob_name.encode()).hexdigest()[:8]
    base = fjob_name[:48]
    return f"{base}-child-{suffix}"


def _fjob_owner_reference(fjob: dict) -> dict:
    meta = fjob.get("metadata", {})
    return {
        "apiVersion": fjob.get("apiVersion", "fournos.dev/v1"),
        "kind": fjob.get("kind", "FournosJob"),
        "name": meta["name"],
        "uid": meta["uid"],
        "controller": False,
        "blockOwnerDeletion": True,
    }


def build_child_job(cfg: dict, fjob: dict) -> dict:
    image = cfg["image"]
    command = cfg.get("command")
    args = cfg.get("args")
    user_env = cfg.get("env") or {}

    child_name = _child_job_name(FJOB_NAME)
    labels = {
        "app.kubernetes.io/managed-by": "fournos-generic",
        "fournos.dev/job-name": FJOB_NAME,
    }

    container: dict = {
        "name": "user-job",
        "image": image,
        "imagePullPolicy": "IfNotPresent",
    }
    if command:
        container["command"] = command if isinstance(command, list) else [command]
    if args:
        container["args"] = args if isinstance(args, list) else [args]
    if user_env:
        container["env"] = [{"name": k, "value": str(v)} for k, v in user_env.items()]

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": child_name,
            "namespace": NAMESPACE,
            "labels": labels,
            "ownerReferences": [_fjob_owner_reference(fjob)],
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": JOB_TIMEOUT,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "serviceAccountName": "fournos",
                    "restartPolicy": "Never",
                    "containers": [container],
                },
            },
        },
    }


def create_child_job(manifest: dict) -> str:
    name = manifest["metadata"]["name"]
    result = sh("kubectl", "create", "-f", "-", input_text=json.dumps(manifest), check=False)
    if result.returncode != 0:
        log(f"ERROR: failed to create child Job: {result.stderr}")
        sys.exit(1)
    log(f"Created child Job {name!r}")
    return name


def wait_for_child(name: str) -> bool:
    log(f"Waiting for child Job {name!r}...")
    deadline = time.monotonic() + JOB_TIMEOUT

    while time.monotonic() < deadline:
        result = sh(
            "kubectl", "get", "job", name, "-n", NAMESPACE,
            "-o", "jsonpath={.status.conditions[*].type}",
            check=False,
        )
        conditions = result.stdout.strip().split()
        if "Complete" in conditions:
            log(f"Child Job {name!r} completed successfully")
            return True
        if "Failed" in conditions:
            log(f"Child Job {name!r} failed")
            return False
        time.sleep(POLL_INTERVAL)

    log(f"Child Job {name!r} timed out after {JOB_TIMEOUT}s")
    return False


def capture_logs(name: str) -> None:
    result = sh("kubectl", "logs", f"job/{name}", "-n", NAMESPACE, "--tail", "-1", check=False)
    if result.stdout:
        print(result.stdout)
        log_file = os.path.join(ARTIFACT_DIR, "child-job.log")
        with open(log_file, "w") as f:
            f.write(result.stdout)
        log(f"Child logs saved to {log_file}")


def cleanup_child(name: str) -> None:
    sh("kubectl", "delete", "job", name, "-n", NAMESPACE, "--ignore-not-found", check=False)
    log(f"Cleaned up child Job {name!r}")


def run(cfg: dict, fjob: dict) -> None:
    manifest = build_child_job(cfg, fjob)
    child_name = create_child_job(manifest)
    try:
        success = wait_for_child(child_name)
        capture_logs(child_name)
        if not success:
            sys.exit(1)
    finally:
        cleanup_child(child_name)


def main() -> None:
    fjob = get_fjob()
    cfg = get_generic_config(fjob)

    if STEP == "resolve-fournos-config":
        resolve(cfg)
    else:
        run(cfg, fjob)


if __name__ == "__main__":
    main()
