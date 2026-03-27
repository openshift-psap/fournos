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
workloads, job listing/filtering, the completion callback, and artifacts.

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

## Deployment

Apply the Kubernetes manifests in order:

```bash
kubectl apply -f config/rbac/
kubectl apply -f config/kueue/
kubectl apply -f config/tekton/
kubectl apply -f config/deployment/
```

## Configuration

All settings are read from environment variables with the `FOURNOS_` prefix:

| Variable | Default | Description |
|---|---|---|
| `FOURNOS_NAMESPACE` | `psap-automation` | Kubernetes namespace |
| `FOURNOS_TEKTON_DASHBOARD_URL` | | Tekton Dashboard base URL |
| `FOURNOS_KUEUE_LOCAL_QUEUE_NAME` | `fournos-queue` | Kueue LocalQueue name |
| `FOURNOS_GPU_RESOURCE_PREFIX` | `fournos/gpu-` | Resource name prefix for GPU types |
| `FOURNOS_ADMISSION_POLL_INTERVAL` | `5.0` | Seconds between admission polls |
| `FOURNOS_ADMISSION_POLL_TIMEOUT` | `3600.0` | Max seconds to wait for admission |
| `FOURNOS_LOG_LEVEL` | `INFO` | Logging level |
