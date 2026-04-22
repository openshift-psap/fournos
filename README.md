# Fournos

> *Fournos* (φούρνος) = "oven" in Greek.

Fournos is a Kubernetes operator that schedules benchmark jobs via
[Kueue](https://kueue.sigs.k8s.io/) and executes them as
[Tekton](https://tekton.dev/) PipelineRuns on remote clusters through the
FORGE framework.

Jobs are submitted as `FournosJob` custom resources. The operator watches
for new CRs, creates Kueue Workloads for quota management, waits for
admission, then launches the corresponding Tekton PipelineRun.

## Cluster dependencies

The following operators must be installed in the cluster before deploying Fournos:

- Red Hat OpenShift Pipelines (`1.21`)
- Red Hat build of Kueue (`1.3`)
- Builds for Red Hat OpenShift Operator (`1.7`)
- Red Hat OpenShift GitOps (`1.20`)
  - only for the GitOps deployment of Fournos

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
  generateName: sample-run-benchamark-
spec:
  owner: perf-team
  displayName: sample-run-benchmark
  cluster: cluster-1
  pipeline: forge-full
  forge:
    project: llmd
    args:
      - cks
    configOverrides:
      batch_size: 64
  env:
    OCPCI_SUITE: regression
    OCPCI_VARIANT: nightly
```

```bash
FOURNOS_NAMESPACE=fournos-$USER-dev
oc create -f config/forge/samples/job-full.yaml -n $FOURNOS_NAMESPACE     # returns the generated name, e.g. forge-full-sample-x7k2m
oc get FournosJobs -n $FOURNOS_NAMESPACE -w            # watch status transitions
oc patch FournosJob <name> -n $FOURNOS_NAMESPACE --type merge -p '{"spec":{"shutdown":"Stop"}}'        # graceful stop (runs finally tasks)
oc patch FournosJob <name> -n $FOURNOS_NAMESPACE --type merge -p '{"spec":{"shutdown":"Terminate"}}'   # immediate terminate (skips finally tasks)
oc delete FournosJob -n $FOURNOS_NAMESPACE <name>      # cleanup
```

### Spec fields

| Field | Required | Description |
|---|---|---|
| `spec.forge.project` | yes | FORGE project path |
| `spec.forge.args` | yes | List of arguments passed to FORGE |
| `spec.forge.configOverrides` | no | Arbitrary YAML overrides passed to the test framework |
| `spec.env` | no | Environment variables passed to the pipeline as a `KEY=VALUE` env file |
| `spec.cluster` | \* | Pin to a specific cluster (Kueue ResourceFlavor) |
| `spec.hardware.gpuType` | \* | Short GPU model name — e.g. `a100`, `h200`. The operator prepends the `FOURNOS_GPU_RESOURCE_PREFIX` (default `fournos/gpu-`) automatically, so do **not** include the full resource path. |
| `spec.hardware.gpuCount` | with gpuType | Number of GPUs (minimum 1) |
| `spec.owner` | no | Team or individual that owns this job |
| `spec.displayName` | no | Human-readable job name (defaults to `metadata.name`) |
| `spec.pipeline` | no | Tekton Pipeline name (default: `fournos-full`) |
| `spec.priority` | no | Kueue WorkloadPriorityClass name |
| `spec.secretRefs` | no | Names of Kubernetes Secrets to mount into the pipeline (references, not values) |
| `spec.exclusive` | no | If `true`, locks the target cluster so no other FournosJob can run there. Requires `spec.cluster`. |
| `spec.shutdown` | no | Shutdown action: `Stop` cancels gracefully (Tekton `CancelledRunFinally` — runs `finally` tasks); `Terminate` cancels immediately (Tekton `Cancelled` — skips `finally` tasks). Both wait for the PipelineRun to finish before releasing Kueue quota. |

\* At least one of `spec.cluster` or `spec.hardware` must be provided. Both can be
set together to pin a hardware request to a specific cluster.

### Status

The operator writes status to `.status`:

| Field | Description |
|---|---|
| `phase` | `Pending` → `Admitted` → `Running` → `Succeeded` / `Failed` / `Stopping` → `Stopped` |
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

Both targets default to the `fournos-local-dev` namespace. Override with
`FOURNOS_NAMESPACE=<YOUR_NAMESPACE> make dev-setup dev-run`.

In another terminal:

```bash
FOURNOS_NAMESPACE=fournos-local-dev make test   # run the integration test suite
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

**FORGE on the hub:** [`config/forge/`](config/forge/) is the real OpenShift configuration for this repo—ImageStreams, Builds, Tekton Tasks and Pipelines, and sample jobs you apply to a cluster. It is **not** the same as the lightweight stand-ins under [`dev/mock-pipelines/`](dev/mock-pipelines/), which [`make dev-setup`](#local-development) installs on kind for local testing only.

Prepare the namespace
```bash
FOURNOS_NAMESPACE=fournos-$USER-dev
oc create ns $FOURNOS_NAMESPACE
oc label ns/$FOURNOS_NAMESPACE fournos.dev/queue-access=true
```

Deploy the operator:

```bash
oc apply -n $FOURNOS_NAMESPACE -f manifests/crd.yaml
for rbac_file in manifests/rbac/*.yaml; do
  cat $rbac_file | NAMESPACE=$FOURNOS_NAMESPACE envsubst | oc apply -f- -n $FOURNOS_NAMESPACE
done
oc apply -n $FOURNOS_NAMESPACE -f manifests/deployment.yaml
```

### Onboarding a new cluster

Three things are needed to make a target cluster available to Fournos:

1. **Create a kubeconfig Secret** so the operator can reach the cluster:

```bash
FOURNOS_NAMESPACE=fournos-$USER-dev
CLUSTER_NAME=<name>
oc create secret generic ${CLUSTER_NAME}-kubeconfig \
  --from-file=kubeconfig=/path/to/auth/kubeconfig \
  -n $FOURNOS_NAMESPACE
```

The secret name must match the `FOURNOS_KUBECONFIG_SECRET_PATTERN` (default
`{cluster}-kubeconfig`).

2. **Add a ResourceFlavor and quota** in `config/kueue-config.yaml`. Add a
   new `ResourceFlavor` with a matching `fournos.dev/cluster` nodeLabel, and
   list it under the `fournos-queue` ClusterQueue with the appropriate GPU/CPU
   quotas. Then apply:

```bash
oc apply -f config/kueue-config.yaml
```

3. **Verify connectivity** by submitting a lightweight validate-only
   job. Edit `cluster` (and optionally `hardware`) in
   `config/fournos-validation/samples/test-connectivity-job.yaml` to
   match the new target, then:

```bash
FOURNOS_NAMESPACE=fournos-$USER-dev
oc create -f config/fournos-validation/samples/test-connectivity-job.yaml -n $FOURNOS_NAMESPACE
oc get fournosjobs -n $FOURNOS_NAMESPACE -w        # should reach Succeeded
```

This runs the `fournos-validate-only` pipeline, which only checks `oc
cluster-info` against the target — no FORGE workload is launched. If the job
reaches `Succeeded`, the kubeconfig secret and Kueue quota are correctly
configured. If it fails, check the operator logs and the PipelineRun status for
details.

### Deploying the FORGE workflow configuration

Apply the production FORGE assets from `config/forge/` (not the kind mocks in `dev/mock-pipelines/`). Deploy the cluster configuration (Builds + Tekton):

```bash
oc apply -n $FOURNOS_NAMESPACE -f config/forge/images/is_forge.yaml
cat config/forge/images/build_forge-main.yaml \
   | sed 's/psap-automation/'$FOURNOS_NAMESPACE'/g' \
   | oc apply -n $FOURNOS_NAMESPACE
oc create -n $FOURNOS_NAMESPACE  -f config/forge/images/buildrun_forge-main.yaml

for wf_file in config/forge/workflows/*.yaml; do
  cat "$wf_file" | NAMESPACE=$FOURNOS_NAMESPACE envsubst '$NAMESPACE' | oc apply -f- -n $FOURNOS_NAMESPACE
done
```

## Configuration

All settings are read from environment variables with the `FOURNOS_` prefix:

| Variable | Default | Description |
|---|---|---|
| `FOURNOS_NAMESPACE` | **required** | Kubernetes namespace |
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

1. **Creates** a Kueue Workload with the requested GPU resources (owned by the FournosJob via `ownerReferences`)
2. **Polls** (5 s timer) for Kueue admission and assigned cluster
3. **Launches** a Tekton PipelineRun with FORGE parameters (owned by the FournosJob via `ownerReferences`)
4. **Watches** the PipelineRun until completion
5. **Deletes** the Workload to release Kueue quota

Setting `spec.shutdown` on a FournosJob triggers cancellation of the
PipelineRun and transitions to `phase=Stopping`. `Stop` uses Tekton's
`CancelledRunFinally` (runs `finally` cleanup tasks); `Terminate` uses
`Cancelled` (skips `finally` tasks). In both cases the operator keeps
the Kueue Workload alive until the PipelineRun finishes, ensuring the
cluster slot is not released prematurely. Once done, the Workload is
deleted and the job moves to `phase=Stopped`.

Deleting a FournosJob automatically cascade-deletes its Workload and
PipelineRun through Kubernetes owner references.

Target clusters need nothing installed — FORGE runs on the hub cluster inside
Tekton Task pods and communicates with targets via `oc`/`kubectl` through
kubeconfig Secrets.

For a detailed breakdown of the CRD, scheduling, operator internals, and key
design decisions, see the [Design Document](Fournos_Design_Document.md).
