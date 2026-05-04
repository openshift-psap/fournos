# Fournos

> *Fournos* (φούρνος) = "oven" in Greek.

Fournos is a Kubernetes operator that schedules benchmark jobs via
[Kueue](https://kueue.sigs.k8s.io/) and executes them as
[Tekton](https://tekton.dev/) PipelineRuns on remote clusters through a
pluggable execution engine.

Jobs are submitted as `FournosJob` custom resources. Every job first
passes through a mandatory **Resolving** phase where a resolve Job (driven
by the configured execution engine) populates GPU requirements and secret
references directly on the FournosJob spec. The operator then creates a
Kueue Workload for quota management, waits for admission, and launches the
corresponding Tekton PipelineRun.

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
  hardware:
    gpuType: a100
    gpuCount: 2
  pipeline: forge-full
  executionEngine:
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
| `spec.executionEngine` | yes | Execution engine configuration. The single top-level key is the engine name (e.g. `forge`); its value is opaque engine-specific config passed through as-is. |
| `spec.env` | no | Environment variables available to the execution engine (read from the FournosJob spec via K8s API) |
| `spec.cluster` | \* | Pin to a specific cluster (Kueue ResourceFlavor). Since `exclusive` defaults to `true`, this also locks the cluster — set `exclusive: false` for shared access. |
| `spec.hardware.gpuType` | \* | Short GPU model name — e.g. `a100`, `h200`. The operator prepends the `FOURNOS_GPU_RESOURCE_PREFIX` (default `fournos/gpu-`) automatically, so do **not** include the full resource path. |
| `spec.hardware.gpuCount` | with gpuType | Number of GPUs (minimum 1) |
| `spec.owner` | no | Team or individual that owns this job |
| `spec.displayName` | no | Human-readable job name (defaults to `metadata.name`) |
| `spec.pipeline` | no | Tekton Pipeline name (default: `fournos-full`). The Pipeline must carry a `fournos.dev/resolve-image` annotation with the full image reference for the resolve Job. |
| `spec.priority` | no | Kueue WorkloadPriorityClass name |
| `spec.secretRefs` | no | Vault-synced K8s Secret names (prefixed with `vault-`) to mount into the pipeline. Populated by the execution engine during the Resolving phase. The operator validates each name in `FOURNOS_SECRETS_NAMESPACE`, copies the secrets into the operator namespace, and mounts them as a projected volume at `/var/run/secrets/fournos/<entry-name>/`. |
| `spec.exclusive` | no (default `true`) | If `true`, locks the target cluster so no other FournosJob can run there. Requires `spec.cluster`. Hardware is optional — when omitted the Workload only requests cluster-slot resources for locking. |
| `spec.shutdown` | no | Shutdown action: `Stop` cancels gracefully (Tekton `CancelledRunFinally` — runs `finally` tasks); `Terminate` cancels immediately (Tekton `Cancelled` — skips `finally` tasks). Both wait for the PipelineRun to finish before releasing Kueue quota. |

\* `spec.hardware` is required unless the job uses exclusive cluster locking
(`exclusive: true` + `cluster`), in which case it may be omitted — the
Workload only needs cluster-slot resources. Every job passes through the
Resolving phase where the execution engine populates `spec.hardware` (if
not already set) and `spec.secretRefs` directly on the FournosJob. Since `exclusive` defaults
to `true`, any job with `spec.cluster` locks the cluster exclusively —
including jobs that also specify `spec.hardware`. Set `exclusive: false` for
shared access (hardware is then required). Jobs without `spec.cluster` must
set `exclusive: false`.

### Status

The operator writes status to `.status`:

| Field | Description |
|---|---|
| `phase` | `Resolving` → `Pending` → `Admitted` → `Running` → `Succeeded` / `Failed` / `Stopping` → `Stopped` |
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
real execution engine runner. The dev environment uses its own Kueue config
(`dev/mock-kueue-config.yaml`) with four mock clusters and synthetic GPU quotas,
plus matching kubeconfig Secrets (`kubeconfig-cluster-{1..4}`) in the dedicated
secrets namespace (`psap-secrets`).

### Before opening a PR

```bash
make lint                        # lint (fournos/ + tests/)
make test                        # integration tests (operator must be running)
```

## Deployment

**Execution engine on the hub:** [`config/forge/`](config/forge/) is the real OpenShift configuration for this repo — ImageStreams, Builds, Tekton Tasks and Pipelines, and sample jobs you apply to a cluster. It is **not** the same as the lightweight stand-ins under [`dev/mock-pipelines/`](dev/mock-pipelines/), which [`make dev-setup`](#local-development) installs on kind for local testing only.

Prepare the namespaces
```bash
FOURNOS_NAMESPACE=fournos-$USER-dev
FOURNOS_SECRETS_NAMESPACE=psap-secrets
oc create ns $FOURNOS_NAMESPACE
oc label ns/$FOURNOS_NAMESPACE fournos.dev/queue-access=true
oc create ns $FOURNOS_SECRETS_NAMESPACE
```

Deploy the operator:

```bash
oc apply -n $FOURNOS_NAMESPACE -f manifests/crd.yaml
for rbac_file in manifests/rbac/*.yaml; do
  cat $rbac_file | NAMESPACE=$FOURNOS_NAMESPACE envsubst | oc apply -f- -n $FOURNOS_NAMESPACE
done
cat manifests/secrets-ns-rbac.yaml \
  | NAMESPACE=$FOURNOS_NAMESPACE SECRETS_NAMESPACE=$FOURNOS_SECRETS_NAMESPACE envsubst \
  | oc apply -f-
oc apply -n $FOURNOS_NAMESPACE -f manifests/deployment.yaml
```

### Onboarding a new cluster

Three things are needed to make a target cluster available to Fournos:

1. **Create a kubeconfig Secret** in the dedicated secrets namespace:

```bash
FOURNOS_SECRETS_NAMESPACE=psap-secrets
CLUSTER_NAME=<name>
oc create secret generic kubeconfig-${CLUSTER_NAME} \
  --from-file=kubeconfig=/path/to/auth/kubeconfig \
  -n $FOURNOS_SECRETS_NAMESPACE
```

The secret name must match the `FOURNOS_KUBECONFIG_SECRET_PATTERN` (default
`kubeconfig-{cluster}`). Secrets are stored in the dedicated namespace
(`FOURNOS_SECRETS_NAMESPACE`, default `psap-secrets`).

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
cluster-info` against the target — no benchmark workload is launched. If the job
reaches `Succeeded`, the kubeconfig secret and Kueue quota are correctly
configured. If it fails, check the operator logs and the PipelineRun status for
details.

### Deploying the execution engine workflow configuration

Apply the production execution engine assets from `config/forge/` (not the kind mocks in `dev/mock-pipelines/`). Deploy the cluster configuration (Builds + Tekton):

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

### Synchronizing secrets from Vault

Pipeline jobs can reference Kubernetes Secrets via `spec.secretRefs`. These
secrets originate in a HashiCorp Vault instance. Because there is no permanent
programmatic access to the vault, secrets are synchronized manually on demand —
whenever the vault content changes.

The sync script reads vault entries and creates one Opaque Secret per entry
in the dedicated secrets namespace (`FOURNOS_SECRETS_NAMESPACE`, default
`psap-secrets`), using a `vault-` prefix followed by the vault entry name as
the K8s Secret name (e.g. vault entry `my-creds` becomes Secret
`vault-my-creds`). Entries whose names are not valid DNS-1123 subdomain
names are skipped with an error. Individual keys within an entry that are
not valid K8s Secret data keys (allowed: alphanumeric, `-`, `_`, `.`) are
also skipped. Existing secrets are updated in-place.

```bash
# 1. Set the required environment variables
export VAULT_ADDR="https://vault.example.com"   # Vault server URL
export VAULT_TOKEN="s.xxxxx"                     # your short-lived token
export VAULT_SECRET_PATH="path/to/secrets"       # directory path within the KV engine

# 2. Sync all vault entries under the configured path
python hacks/sync_vault_secrets.py -n psap-secrets

# 3. Preview without touching the cluster
python hacks/sync_vault_secrets.py -n psap-secrets --dry-run
```

Makefile shortcuts (`VAULT_ADDR`, `VAULT_TOKEN`, and `VAULT_SECRET_PATH` must be set):

```bash
make sync-vault-secrets              # syncs all entries
make sync-vault-secrets-dry-run      # preview only
```

The synced secrets are labelled `fournos.dev/vault-entry=true` and
`app.kubernetes.io/managed-by=fournos-vault-sync` for easy identification.
Secret references are populated by the execution engine during the Resolving phase directly
on the FournosJob `spec.secretRefs` field. The operator validates each
referenced Secret exists in the secrets namespace and carries the vault
label during the Resolving phase, then copies them into the operator
namespace during the Admitted phase and mounts them as a projected volume
into the PipelineRun pods. Each secret's keys are placed under a
subdirectory matching the original name:

```
/var/run/secrets/fournos/
  vault-my-creds/
    username
    password
  vault-other-creds/
    token
```

Copied secrets are named `<fjob-name>-<secret-name>` and carry
`ownerReferences` back to the FournosJob, so Kubernetes garbage-collects
them automatically when the job is deleted.

## Configuration

All settings are read from environment variables with the `FOURNOS_` prefix:

| Variable | Default | Description |
|---|---|---|
| `FOURNOS_NAMESPACE` | **required** | Kubernetes namespace |
| `FOURNOS_SECRETS_NAMESPACE` | `psap-secrets` | Namespace where kubeconfig and vault-synced secrets are stored |
| `FOURNOS_TEKTON_DASHBOARD_URL` | | Tekton Dashboard base URL |
| `FOURNOS_KUBECONFIG_SECRET_PATTERN` | `kubeconfig-{cluster}` | Pattern for resolving cluster names to Secret names |
| `FOURNOS_VAULT_SECRET_PATTERN` | `vault-{entry}` | Pattern for naming vault-synced Secrets |
| `FOURNOS_KUEUE_LOCAL_QUEUE_NAME` | `fournos-queue` | Kueue LocalQueue name |
| `FOURNOS_GPU_RESOURCE_PREFIX` | `fournos/gpu-` | Resource name prefix for GPU types |
| `FOURNOS_LOG_LEVEL` | `INFO` | Logging level |
| `FOURNOS_GC_INTERVAL_SEC` | `300` | Resource GC interval (seconds) |
| `FOURNOS_RESOLVE_DEADLINE_SEC` | `300` | Deadline for the resolve Job (seconds) |
| `FOURNOS_RESOLVE_JOB_TEMPLATE` | `config/forge/resolve_job.yaml` | Path (relative to project root) to the Job YAML template for the resolve step. Override with `dev/mock-resolve/resolve_job.yaml` for local dev/CI. |
| `FOURNOS_ARTIFACT_PVC_SIZE` | `1Gi` | Size of the per-PipelineRun PVC used for shared artifact storage across pipeline tasks |

## Architecture

```
FournosJob CR ──→ Operator ──→ Resolve Job (e.g. FORGE, patches FournosJob spec) ──→ Kueue Workload ──→ (admission) ──→ Tekton PipelineRun ──→ Execution Engine (e.g. FORGE) ──→ target cluster
```

The operator runs as a single-replica Deployment using
[kopf](https://kopf.dev/). On each `FournosJob`, it:

1. **Resolves** job requirements by launching a resolve K8s Job (using the configured execution engine image) that populates the FournosJob spec with GPU type/count and secret references
2. **Creates** a Kueue Workload with the resolved GPU resources (owned by the FournosJob via `ownerReferences`)
3. **Polls** (5 s timer) for Kueue admission and assigned cluster
4. **Copies** referenced Vault secrets from the secrets namespace into the operator namespace (per-job copies with `ownerReferences` for automatic cleanup) and **launches** a Tekton PipelineRun with `FJOB_NAME` + `FOURNOS_NAMESPACE` (so the execution engine can look up the full FournosJob spec), the secrets mounted as a projected volume at `/var/run/secrets/fournos/` (owned by the FournosJob via `ownerReferences`), and a shared `artifacts` workspace backed by a `volumeClaimTemplate` PVC for cross-task artifact storage (managed by Tekton)
5. **Watches** the PipelineRun until completion
6. **Deletes** the Workload to release Kueue quota

Setting `spec.shutdown` on a FournosJob triggers cancellation of the
PipelineRun and transitions to `phase=Stopping`. `Stop` uses Tekton's
`CancelledRunFinally` (runs `finally` cleanup tasks); `Terminate` uses
`Cancelled` (skips `finally` tasks). In both cases the operator keeps
the Kueue Workload alive until the PipelineRun finishes, ensuring the
cluster slot is not released prematurely. Once done, the Workload is
deleted and the job moves to `phase=Stopped`.

Deleting a FournosJob automatically cascade-deletes its Workload and
PipelineRun through Kubernetes owner references.

Target clusters need nothing installed — the execution engine runs on the hub
cluster inside Tekton Task pods and communicates with targets via
`oc`/`kubectl` through kubeconfig Secrets.

For a detailed breakdown of the CRD, scheduling, operator internals, and key
design decisions, see the [Design Document](Fournos_Design_Document.md).
