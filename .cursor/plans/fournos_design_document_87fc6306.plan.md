---
name: Fournos Implementation
overview: "Fournos Python HTTP service: API endpoints, Kueue/Tekton core logic, K8s manifests, local dev environment, and e2e test suite."
todos:
  - id: 1-project-scaffold
    content: "Project scaffolding: pyproject.toml, Dockerfile, README.md, .gitignore, .dockerignore, Makefile, pre-commit config"
    status: complete
  - id: 2-api-models
    content: "Pydantic models: JobSubmitRequest, JobStatusResponse, JobListResponse, ArtifactsResponse, HardwareRequest, ForgeConfig, JobStatus enum"
    status: complete
  - id: 3-api-post-jobs
    content: "POST /api/v1/jobs: validate request, branch into Mode A (explicit cluster) or Mode B (hardware request via Kueue)"
    status: complete
  - id: 4-api-get-job-status
    content: "GET /api/v1/job/{id}: query PipelineRun then Workload, ?wait=true long-poll, Tekton Dashboard URL"
    status: complete
  - id: 5-api-list-jobs
    content: "GET /api/v1/jobs: list all jobs from PipelineRuns + Workloads, optional ?status= filter"
    status: complete
  - id: 6-api-complete
    content: "POST /api/v1/job/{id}/complete: callback endpoint for Tekton finally-task to release Kueue Workload quota"
    status: complete
  - id: 7-api-get-artifacts
    content: "GET /api/v1/job/{id}/artifacts: stub returning job ID (artifact retrieval TBD)"
    status: complete
  - id: 8-core-clusters
    content: "core/clusters.py: ClusterRegistry — resolve kubeconfig Secrets, check cluster existence"
    status: complete
  - id: 9-core-tekton
    content: "core/tekton.py: TektonClient — create, get, list PipelineRuns; extract status from conditions"
    status: complete
  - id: 10-core-kueue
    content: "core/kueue.py: KueueClient — create, get, list, delete Workloads; poll admission; read assigned flavor"
    status: complete
  - id: 11-kueue-manifests
    content: "config/kueue/ manifests: ResourceFlavors, ClusterQueue, LocalQueue, WorkloadPriorityClasses (v1beta2)"
    status: complete
  - id: 12-tekton-manifests
    content: "config/tekton/ manifests: shared Tasks (prepare, run, cleanup, notify), fournos-full Pipeline, fournos-run-only Pipeline"
    status: complete
  - id: 13-rbac-manifests
    content: "config/rbac/ manifests: ClusterRole + ClusterRoleBinding for Kueue resources"
    status: complete
  - id: 14-dockerfile
    content: "Dockerfile: Python base, install deps, copy source, uvicorn entrypoint"
    status: complete
  - id: 15-deployment-manifests
    content: "config/deployment/ manifests: Deployment + Service in psap-automation namespace"
    status: complete
  - id: 16-local-dev
    content: "Local dev environment: kind cluster setup script, mock resources, Makefile targets (dev-setup, dev-run, dev-test, dev-teardown)"
    status: complete
  - id: 17-testing
    content: "E2e pytest test suite: tests/ directory with httpx-based tests against a live Fournos instance"
    status: complete
isProject: true
---

# Fournos Design Document (Tekton + Kueue)

## 1. Introduction

*Fournos* (φούρνος) = "oven" in Greek. A Python-based HTTP service (FastAPI) that accepts benchmark jobs, schedules them via Kueue, and executes them as Tekton PipelineRuns on remote clusters through the FORGE framework.

## 2. Architecture overview

```mermaid
flowchart LR
    Triggers["Triggers\n(OCPCI, GitHub Actions, curl)"] --> Fournos
    subgraph Hub["Hub cluster (psap-automation)"]
        Fournos["Fournos API\n(FastAPI)"]
        Kueue["Kueue"]
        Tekton["Tekton Pipelines"]
        FORGE["FORGE\n(in Tekton Tasks)"]
        Fournos --> Kueue
        Fournos --> Tekton
        Tekton --> FORGE
    end
    FORGE -- "remote oc/kubectl\nvia kubeconfig Secrets" --> Target1["Target cluster 1"]
    FORGE -- "remote oc/kubectl\nvia kubeconfig Secrets" --> Target2["Target cluster 2"]
```



- **Hub cluster**: hosts Fournos, Kueue, Tekton Pipelines, and FORGE (running inside Tekton Task pods) in the `psap-automation` namespace
- **Target clusters**: nothing installed — FORGE runs on the hub cluster and communicates with targets via remote `oc`/`kubectl` commands using kubeconfig Secrets

## 3. API

### POST /api/v1/jobs — submit a job (201)

Request body:


| Field      | Type   | Required | Description                                                 |
| ---------- | ------ | -------- | ----------------------------------------------------------- |
| `name`     | string | yes      | Job name                                                    |
| `pipeline` | string | no       | Tekton Pipeline name (default: `fournos-full`)              |
| `cluster`  | string | one of   | Explicit cluster name — Mode A, bypasses Kueue              |
| `hardware` | object | one of   | `{gpu_type, gpu_count}` — Mode B, scheduled via Kueue       |
| `forge`    | object | yes      | `{project, preset, args[]}` — passed through to Tekton Task |
| `secrets`  | list   | no       | Secret names to mount in the runner pod                     |
| `priority` | string | no       | Kueue `WorkloadPriorityClass` name (Mode B only)            |


Returns `JobStatusResponse`.

### GET /api/v1/jobs — list jobs (200)

Query params: `?status=pending|admitted|running|succeeded|failed` (optional filter).

Merges PipelineRuns and Kueue Workloads (deduplicated by job ID). Returns `JobListResponse` with `jobs[]` and `count`.

### GET /api/v1/job/{id} — job status (200)

Query params: `?wait=true` for long-poll until terminal state.

Looks up PipelineRun first, falls back to Kueue Workload. Returns `JobStatusResponse` with fields: `id`, `name`, `status`, `cluster`, `pipeline_run`, `dashboard_url`.

### POST /api/v1/job/{id}/complete — completion callback (204)

Called by the Tekton `fournos-notify` finally-task (or externally) when a pipeline finishes. Deletes the Kueue Workload to release quota. Idempotent — safe to call repeatedly or for Mode A jobs (no-op).

### GET /api/v1/job/{id}/artifacts — artifacts (200)

Returns `ArtifactsResponse` with `id`, `artifacts[]`, `mlflow_url`. Currently a stub that returns the job ID only.

### GET /healthz — health check (200)

Returns `{"status": "ok"}`.

## 4. Scheduling modes

### Mode A: explicit cluster

User specifies `cluster: "cluster-1"`. Fournos resolves the kubeconfig Secret (`{cluster}-kubeconfig`), verifies it exists, and immediately creates a Tekton PipelineRun. No Kueue involvement.

### Mode B: hardware request

User specifies `hardware: {gpu_type: "A100", gpu_count: 2}`. Fournos creates a virtual Kueue Workload with the requested resources. A background coroutine polls for admission. On admission, Fournos reads the assigned flavor (= cluster name), resolves the kubeconfig Secret, and creates the PipelineRun. When the pipeline finishes, the `fournos-notify` finally-task calls `POST /api/v1/job/{id}/complete` to delete the Workload and release quota.

```mermaid
sequenceDiagram
    participant Client
    participant Fournos
    participant Kueue
    participant Tekton

    Client->>Fournos: POST /jobs (Mode B)
    Fournos->>Kueue: create Workload
    Fournos-->>Client: 201 pending

    Note over Fournos,Kueue: background: poll admission
    Kueue-->>Fournos: admitted (flavor=cluster-2)
    Fournos->>Tekton: create PipelineRun

    Note over Tekton: pipeline runs...
    Tekton->>Fournos: POST /job/{id}/complete
    Fournos->>Kueue: delete Workload
```



## 5. Persistence

Job state is stored entirely in Kubernetes resources — no in-memory store:

- **Tekton PipelineRuns**: carry job ID and name as labels/annotations; status derived from conditions
- **Kueue Workloads**: carry job ID and name as labels/annotations; admission state from conditions and `status.admission.podSetAssignments`

Listing/status endpoints query these resources directly and merge them.

## 6. FORGE integration

FORGE is an existing benchmark execution framework that runs on the hub cluster inside Tekton Task pods and owns all operations on target clusters — setup, benchmark execution, and cleanup — by issuing remote `oc`/`kubectl` commands via kubeconfig Secrets. Fournos has a strict separation of concerns: it handles cluster selection, scheduling, and bookkeeping, but never interacts with target clusters directly. All FORGE parameters (`project`, `preset`, `args`) are passed through opaquely to the Tekton Pipeline as params. Fournos also passes `job-id` and `job-name` so FORGE can use them for its own resource naming and correlation.

The Tekton Task definitions in `config/tekton/tasks.yaml` are stub implementations showing the expected parameter interface. The real FORGE tasks will replace them.

## 7. Tekton Pipelines and Tasks

### Tasks ([config/tekton/tasks.yaml](config/tekton/tasks.yaml))

FORGE-owned tasks (stubs in this repo, replaced by real FORGE implementation):


| Task              | Description                                     |
| ----------------- | ----------------------------------------------- |
| `fournos-prepare` | FORGE: set up the target cluster                |
| `fournos-run`     | FORGE: run the benchmark on the target cluster  |
| `fournos-cleanup` | FORGE: clean up resources on the target cluster |


Fournos-owned task:


| Task             | Description                                                 |
| ---------------- | ----------------------------------------------------------- |
| `fournos-notify` | POST to Fournos `/complete` endpoint to release Kueue quota |


### Pipelines


| Pipeline           | File                                                           | Tasks         | Finally                 |
| ------------------ | -------------------------------------------------------------- | ------------- | ----------------------- |
| `fournos-full`     | [pipeline-full.yaml](config/tekton/pipeline-full.yaml)         | prepare → run | cleanup, notify-fournos |
| `fournos-run-only` | [pipeline-run-only.yaml](config/tekton/pipeline-run-only.yaml) | run           | notify-fournos          |


The `pipeline` field in `JobSubmitRequest` selects which pipeline to use (default: `fournos-full`).

## 8. Kueue configuration

[config/kueue/kueue-config.yaml](config/kueue/kueue-config.yaml):

- **ResourceFlavors**: one per cluster (abstract quota buckets mapped to cluster names)
- **ClusterQueue** `fournos-queue`: per-cluster GPU quotas using virtual resource `fournos/gpu-{type}`
- **LocalQueue** in `psap-automation` namespace
- **WorkloadPriorityClasses** (v1beta2): `manual`, `nightly`, `presubmit`, `adhoc`

## 9. Deployment

Namespace-scoped tenant on a shared OpenShift management cluster:

- [config/rbac/](config/rbac/) — ClusterRole + ClusterRoleBinding for Kueue cluster resources
- [config/deployment/](config/deployment/) — Deployment + Service in `psap-automation`
- [Dockerfile](Dockerfile) — Python base image, pip install, uvicorn entrypoint

```bash
kubectl apply -f config/rbac/
kubectl apply -f config/kueue/
kubectl apply -f config/tekton/
kubectl apply -f config/deployment/
```

## 10. Configuration

All settings via environment variables with `FOURNOS_` prefix ([fournos/settings.py](fournos/settings.py)):


| Variable                            | Default                | Description                       |
| ----------------------------------- | ---------------------- | --------------------------------- |
| `FOURNOS_NAMESPACE`                 | `psap-automation`      | Kubernetes namespace              |
| `FOURNOS_TEKTON_DASHBOARD_URL`      | *(empty)*              | Tekton Dashboard base URL         |
| `FOURNOS_KUBECONFIG_SECRET_PATTERN` | `{cluster}-kubeconfig` | Secret name pattern               |
| `FOURNOS_KUEUE_LOCAL_QUEUE_NAME`    | `fournos-queue`        | Kueue LocalQueue name             |
| `FOURNOS_GPU_RESOURCE_PREFIX`       | `fournos/gpu-`         | Virtual resource name prefix      |
| `FOURNOS_ADMISSION_POLL_INTERVAL`   | `5.0`                  | Seconds between admission polls   |
| `FOURNOS_ADMISSION_POLL_TIMEOUT`    | `3600.0`               | Max seconds to wait for admission |
| `FOURNOS_LOG_LEVEL`                 | `INFO`                 | Logging level                     |


## 11. Project structure

```
fournos/
  app.py                   # FastAPI app factory, lifespan (K8s client init)
  settings.py              # Pydantic Settings (env vars)
  models.py                # Request/response Pydantic models
  api/v1/
    router.py              # APIRouter aggregating jobs + artifacts
    jobs.py                # POST /jobs, GET /jobs, GET /job/{id}, POST /job/{id}/complete
    artifacts.py           # GET /job/{id}/artifacts
  core/
    constants.py           # Shared label keys
    clusters.py            # ClusterRegistry (Secret lookup)
    tekton.py              # TektonClient (PipelineRun CRUD)
    kueue.py               # KueueClient (Workload CRUD, admission polling)
config/
  kueue/                   # ClusterQueue, ResourceFlavors, LocalQueue, WorkloadPriorityClasses
  tekton/                  # Tasks, fournos-full Pipeline, fournos-run-only Pipeline
  rbac/                    # ClusterRole, ClusterRoleBinding
  deployment/              # Deployment, Service
dev/
  setup.sh                 # kind cluster setup (Tekton + Kueue + mock resources)
  mock-resources.yaml      # Mock Tasks, Pipelines, kubeconfig Secrets
tests/
  conftest.py              # Fixtures (httpx client, helpers)
  test_e2e.py              # E2e tests against a live Fournos instance
Dockerfile
Makefile                   # dev-setup, dev-run, dev-test, dev-teardown, lint, format
pyproject.toml
.pre-commit-config.yaml    # ruff lint + format hooks
README.md
```

## 12. Key design decisions

- **Python HTTP service** (FastAPI), not a Go controller or CRD-based operator
- **Two scheduling modes**: explicit cluster (bypass Kueue) and hardware request (through Kueue)
- **Separation of concerns** — Fournos owns scheduling, bookkeeping, and parameter passing; FORGE owns all target-cluster operations (setup, execution, cleanup). Fournos never touches target clusters directly.
- **FORGE is opaque** — Fournos never validates FORGE config, just passes parameters through to the Tekton Pipeline
- **Tekton for execution, Kueue for scheduling** — virtual Workload pattern with `fournos/gpu-`* resources
- **Stateless service** — all job state lives in Kubernetes resources (PipelineRuns, Workloads), not in memory
- **Completion callback** — Tekton `finally` task notifies Fournos instead of Fournos polling each PipelineRun
- **Multiple pipelines** — `fournos-full` (prepare → run → cleanup) and `fournos-run-only` (run only), selectable per job
- **Target clusters need nothing installed** — FORGE runs on the hub cluster inside Tekton Task pods and communicates with targets via remote `oc`/`kubectl` commands through kubeconfig Secrets

