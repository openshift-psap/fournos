# Fournos

> *Fournos* (φούρνος) = "oven" in Greek.

Fournos is a Kubernetes operator that schedules benchmark jobs via
[Kueue](https://kueue.sigs.k8s.io/) and executes them as
[Tekton](https://tekton.dev/) PipelineRuns on remote clusters through the
FORGE framework.

Jobs are submitted as `FournosJob` custom resources. The operator watches
for new CRs, creates Kueue Workloads for quota management, waits for
admission, then launches the corresponding Tekton PipelineRun.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
```

## Submitting a job

Create a `FournosJob` resource. Use `generateName` for automatic unique naming
and `displayName` for a human-readable label:

```yaml
apiVersion: fournos.dev/v1
kind: FournosJob
metadata:
  generateName: nightly-llama3-
  namespace: psap-automation
spec:
  owner: perf-team
  displayName: nightly-llama3-benchmark
  cluster: cluster-1
  forge:
    project: testproj/llmd
    preset: cks
  env:
    OCPCI_SUITE: regression
    OCPCI_VARIANT: nightly
```

```bash
kubectl create -f job.yaml                               # returns the generated name, e.g. nightly-llama3-x7k2m
kubectl get fournosjobs -n psap-automation  -w           # watch status transitions
kubectl delete fournosjob -n psap-automation  <name>     # cleanup
```

### Spec fields

| Field | Required | Description |
|---|---|---|
| `spec.forge.project` | yes | FORGE project path |
| `spec.forge.preset` | yes | FORGE preset name |
| `spec.forge.configOverrides` | no | Key-value overrides passed to the test framework |
| `spec.env` | no | Environment variables for test identification (e.g. OCPCI suite/variant) |
| `spec.cluster` | \* | Pin to a specific cluster (Kueue ResourceFlavor) |
| `spec.hardware.gpuType` | \* | GPU model (e.g. `A100`, `H200`) |
| `spec.hardware.gpuCount` | with gpuType | Number of GPUs (minimum 1) |
| `spec.owner` | no | Team or individual that owns this job |
| `spec.displayName` | no | Human-readable job name (defaults to `metadata.name`) |
| `spec.pipeline` | no | Tekton Pipeline name (default: `fournos-full`) |
| `spec.priority` | no | Kueue WorkloadPriorityClass name |
| `spec.secrets` | no | Additional Secret names for the pipeline |

\* At least one of `spec.cluster` or `spec.hardware` must be provided. Both can be
set together to pin a hardware request to a specific cluster.

### Status

The operator writes status to `.status`:

| Field | Description |
|---|---|
| `phase` | `Pending` → `Admitted` → `Running` → `Succeeded` / `Failed` |
| `cluster` | Cluster assigned by Kueue |
| `pipelineRun` | Name of the Tekton PipelineRun |
| `dashboardURL` | Tekton Dashboard link (if configured) |
| `message` | Error details on failure |

## Local development

Prerequisites: [Podman](https://podman.io/),
[kind](https://kind.sigs.k8s.io/), and `kubectl`.

```bash
make dev-setup    # creates a kind cluster, installs Tekton + Kueue + CRD, applies mock resources
make dev-run      # starts the operator locally (connects to the kind cluster)
```

In another terminal:

```bash
make test                              # run the integration test suite
```

```bash
make dev-teardown # deletes the kind cluster
```

`dev-setup` installs real Tekton Pipelines and Kueue controllers into the kind
cluster, but substitutes lightweight mock Tasks (echo + sleep) in place of the
real FORGE runner. The dev environment uses its own Kueue config
(`dev/mock-kueue-config.yaml`) with four mock clusters and synthetic GPU quotas,
plus matching kubeconfig Secrets (`cluster-{1..4}-kubeconfig`).

### Before opening a PR

```bash
make lint                        # lint (fournos/ + tests/)
make test                        # integration tests (operator must be running)
```

## Deployment

Apply the Kubernetes manifests in order:

```bash
kubectl apply -f manifests/crd.yaml
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
| `FOURNOS_KUBECONFIG_SECRET_PATTERN` | `{cluster}-kubeconfig` | Pattern for resolving cluster names to Secret names |
| `FOURNOS_KUEUE_LOCAL_QUEUE_NAME` | `fournos-queue` | Kueue LocalQueue name |
| `FOURNOS_GPU_RESOURCE_PREFIX` | `fournos/gpu-` | Resource name prefix for GPU types |
| `FOURNOS_LOG_LEVEL` | `INFO` | Logging level |
| `FOURNOS_GC_INTERVAL_SEC` | `300` | Resource GC interval (seconds) |

## Architecture

```
FournosJob CR ──→ Operator ──→ Kueue Workload ──→ (admission) ──→ Tekton PipelineRun ──→ FORGE ──→ target cluster
```

The operator runs as a single-replica Deployment using
[kopf](https://kopf.dev/). On each `FournosJob`, it:

1. **Creates** a Kueue Workload with the requested GPU resources
2. **Polls** (5 s timer) for Kueue admission and assigned cluster
3. **Launches** a Tekton PipelineRun with FORGE parameters
4. **Watches** the PipelineRun until completion
5. **Deletes** the Workload to release Kueue quota

Target clusters need nothing installed — FORGE runs on the hub cluster inside
Tekton Task pods and communicates with targets via `oc`/`kubectl` through
kubeconfig Secrets.
