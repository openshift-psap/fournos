# Fournos

> *Fournos* (φούρνος) = "oven" in Greek.

Fournos is a job scheduling HTTP service that accepts benchmark jobs, schedules them via
[Kueue](https://kueue.sigs.k8s.io/), and executes them as
[Tekton](https://tekton.dev/) PipelineRuns on remote clusters through the
FORGE framework.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
```

This installs a Git pre-commit hook that runs `ruff` (lint + format) on every
commit.

## Local development

Prerequisites: [Podman](https://podman.io/),
[kind](https://kind.sigs.k8s.io/), and `kubectl`.

```bash
make dev-setup    # creates a kind cluster, installs Tekton + Kueue, applies mock resources
make dev-run      # starts Fournos locally (connects to the kind cluster)
make dev-test     # runs the e2e pytest suite against the running instance
make dev-teardown # deletes the kind cluster
```

`dev-setup` installs real Tekton Pipelines and Kueue controllers into the kind
cluster, but substitutes lightweight mock Tasks (echo + sleep) in place of the
real FORGE runner. Four mock kubeconfig Secrets (`cluster-{1..4}-kubeconfig`)
are created so both scheduling modes work end-to-end.

### Testing

The e2e test suite lives in `tests/` and uses pytest + httpx against a live
Fournos instance. Start the server with `make dev-run`, then in another
terminal:

```bash
make dev-test                                    # default: http://localhost:8000
FOURNOS_URL=http://other-host:8000 make dev-test # override target
```

The tests cover both submission modes, Kueue admission polling, inadmissible
workloads, job listing/filtering, the completion callback, artifacts, and
reconciler cleanup of orphaned/stale Workloads.

### Before opening a PR

Run the full lint and test suite locally before pushing:

```bash
ruff check fournos/ tests/       # lint
ruff format --check fournos/ tests/  # formatting
make dev-test                    # e2e tests (requires dev-setup + dev-run)
```

CI runs the same checks via the `pull_request` workflow and will block merge on
failures. Catching issues locally avoids unnecessary round-trips.

## API

### Submit a job

```bash
# Mode A — explicit cluster (bypasses Kueue)
curl -X POST http://localhost:8000/api/v1/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-benchmark",
    "cluster": "cluster-1",
    "forge": {"project": "testproj/llmd", "preset": "cks"}
  }'

# Mode B — hardware request (scheduled via Kueue)
curl -X POST http://localhost:8000/api/v1/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-benchmark",
    "hardware": {"gpu_type": "A100", "gpu_count": 2},
    "forge": {"project": "testproj/llmd", "preset": "llama3"},
    "priority": "nightly"
  }'
```

### List jobs

```bash
curl http://localhost:8000/api/v1/jobs                # all jobs
curl http://localhost:8000/api/v1/jobs?status=running  # filter by status
curl http://localhost:8000/api/v1/jobs?status=pending
```

### Check job status

```bash
curl http://localhost:8000/api/v1/job/{id}
curl http://localhost:8000/api/v1/job/{id}?wait=true   # long-poll
```

### Signal job completion

Called by the Tekton `finally` task (or externally) to release the Kueue
Workload quota after a pipeline finishes. Idempotent — safe to call multiple
times or for Mode A jobs that have no Workload.

```bash
curl -X POST http://localhost:8000/api/v1/job/{id}/complete   # returns 204
```

### Get artifacts

```bash
curl http://localhost:8000/api/v1/job/{id}/artifacts
```

## Reconciler

A background reconciler loop scans for Kueue Workloads that are leaking quota
and deletes them. It runs every `FOURNOS_RECONCILE_INTERVAL_SEC` (default 60
seconds) and handles two cases:

- **Orphaned Workloads** — admitted but no corresponding PipelineRun exists
  (e.g. the admission-polling task was lost after a process restart).
- **Stale Workloads** — a PipelineRun reached a terminal state but the
  `fournos-notify` completion callback failed to delete the Workload.

Only Workloads that have been admitted for at least `2 × reconcile_interval` are
eligible for cleanup, avoiding races with the normal fast-path. Pending
Workloads (waiting for cluster resources) are never touched.

## Deployment

Apply the Kubernetes manifests in order:

```bash
kubectl apply -f manifests/rbac.yaml
kubectl apply -f manifests/kueue-config.yaml
kubectl apply -f manifests/tekton/
kubectl apply -f manifests/deployment.yaml
```

## Configuration

All settings are read from environment variables with the `FOURNOS_` prefix:

| Variable | Default | Description |
|---|---|---|
| `FOURNOS_NAMESPACE` | `psap-automation` | Kubernetes namespace |
| `FOURNOS_TEKTON_DASHBOARD_URL` | | Tekton Dashboard base URL |
| `FOURNOS_KUBECONFIG_SECRET_PATTERN` | `{cluster}-kubeconfig` | Pattern for resolving cluster names to Kubernetes Secret names (see below) |
| `FOURNOS_KUEUE_LOCAL_QUEUE_NAME` | `fournos-queue` | Kueue LocalQueue name |
| `FOURNOS_GPU_RESOURCE_PREFIX` | `fournos/gpu-` | Resource name prefix for GPU types |
| `FOURNOS_ADMISSION_POLL_INTERVAL_SEC` | `5.0` | Seconds between admission polls |
| `FOURNOS_ADMISSION_POLL_TIMEOUT_SEC` | `3600.0` | Max seconds to wait for admission |
| `FOURNOS_RECONCILE_INTERVAL_SEC` | `60.0` | Seconds between reconciler scans |
| `FOURNOS_LOG_LEVEL` | `INFO` | Logging level |

### Cluster-to-kubeconfig mapping

Fournos uses a convention-based mapping to resolve cluster names to kubeconfig
Secrets. There is no explicit cluster registry — a cluster is considered
available if a matching Secret exists in the Fournos namespace.

The Secret name is derived from the cluster name using
`FOURNOS_KUBECONFIG_SECRET_PATTERN` (default `{cluster}-kubeconfig`). For
example, a cluster named `gpu-cluster-01` resolves to a Secret named
`gpu-cluster-01-kubeconfig`. In Mode A the resolution happens at job submission
time; in Mode B (Kueue-routed) it happens after admission, using the Kueue
ResourceFlavor name as the cluster name.

Each Tekton task mounts the resolved Secret at `/workspace/kubeconfig/kubeconfig`
so that `kubectl` and `forge` commands target the correct remote cluster.
