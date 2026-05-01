# Fournos

> *Fournos* (φούρνος) = "oven" in Greek.

Fournos is a Kubernetes operator that schedules benchmark jobs via
[Kueue](https://kueue.sigs.k8s.io/) and executes them as
[Tekton](https://tekton.dev/) PipelineRuns on remote clusters through the
FORGE framework.

Jobs are submitted as `FournosJob` custom resources. Every job first
passes through a mandatory **Resolving** phase where a Forge Job populates
GPU requirements and secret references directly on the FournosJob spec. The
operator then creates a Kueue Workload for quota management, waits for
admission, and launches the corresponding Tekton PipelineRun.

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
| `spec.env` | no | Environment variables available to FORGE (read from the FournosJob spec via K8s API) |
| `spec.cluster` | \* | Pin to a specific cluster (Kueue ResourceFlavor). Since `exclusive` defaults to `true`, this also locks the cluster — set `exclusive: false` for shared access. |
| `spec.hardware.gpuType` | \* | Short GPU model name — e.g. `a100`, `h200`. The operator prepends the `FOURNOS_GPU_RESOURCE_PREFIX` (default `fournos/gpu-`) automatically, so do **not** include the full resource path. |
| `spec.hardware.gpuCount` | with gpuType | Number of GPUs (minimum 1) |
| `spec.owner` | no | Team or individual that owns this job |
| `spec.displayName` | no | Human-readable job name (defaults to `metadata.name`) |
| `spec.pipeline` | no | Tekton Pipeline name (default: `fournos-full`) |
| `spec.priority` | no | Kueue WorkloadPriorityClass name |
| `spec.secretRefs` | no | Vault-synced K8s Secret names (prefixed with `vault-`) to mount into the pipeline. Populated by Forge during the Resolving phase. The operator validates each name in `FOURNOS_SECRETS_NAMESPACE`, copies the secrets into the operator namespace, and mounts them as a projected volume at `/var/run/secrets/fournos/<entry-name>/`. |
| `spec.exclusive` | no (default `true`) | If `true`, locks the target cluster so no other FournosJob can run there. Requires `spec.cluster`. Hardware is optional — when omitted the Workload only requests cluster-slot resources for locking. |
| `spec.shutdown` | no | Shutdown action: `Stop` cancels gracefully (Tekton `CancelledRunFinally` — runs `finally` tasks); `Terminate` cancels immediately (Tekton `Cancelled` — skips `finally` tasks). Both wait for the PipelineRun to finish before releasing Kueue quota. |

\* `spec.hardware` is required unless the job uses exclusive cluster locking
(`exclusive: true` + `cluster`), in which case it may be omitted — the
Workload only needs cluster-slot resources. Every job passes through the
Resolving phase where Forge populates `spec.hardware` (if not already set)
and `spec.secretRefs` directly on the FournosJob. Since `exclusive` defaults
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
real FORGE runner. The dev environment uses its own Kueue config
(`dev/mock-kueue-config.yaml`) with four mock clusters and synthetic GPU quotas,
plus matching kubeconfig Secrets (`kubeconfig-cluster-{1..4}`) in the dedicated
secrets namespace (`psap-secrets`).

### Before opening a PR

```bash
make lint                        # lint (fournos/ + tests/)
make test                        # integration tests (operator must be running)
```

## Deployment

**FORGE on the hub:** [`config/forge/`](config/forge/) is the real OpenShift configuration for this repo—ImageStreams, Builds, Tekton Tasks and Pipelines, and sample jobs you apply to a cluster. It is **not** the same as the lightweight stand-ins under [`dev/mock-pipelines/`](dev/mock-pipelines/), which [`make dev-setup`](#local-development) installs on kind for local testing only.

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
Secret references are populated by Forge during the Resolving phase directly
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

## PSAPCluster — Cluster Management

The `PSAPCluster` custom resource provides a single pane of glass for cluster
state, GPU inventory, and ownership locking.

### Viewing clusters

```bash
oc get psapclusters -n psap-automation
```

```
NAME          OWNER   GPUS      KUBECONFIG    LOCKED   AGE
athena-fire           8x H200   Valid         false    3h
psap-mgmt                       Valid         false    3h
```

### Onboarding a cluster via PSAPCluster

1. Create a kubeconfig Secret in the secrets namespace (default `psap-secrets`):

```bash
oc create secret generic kubeconfig-<cluster-name> \
  --from-file=kubeconfig=/path/to/kubeconfig \
  -n psap-secrets
```

2. Add a ResourceFlavor and quota for the cluster in
   `config/kueue-cluster-config.yaml` (see [Onboarding a new cluster](#onboarding-a-new-cluster)).

3. Create a `PSAPCluster` resource:

```yaml
apiVersion: fournos.dev/v1
kind: PSAPCluster
metadata:
  name: my-cluster
spec:
  kubeconfigSecret: kubeconfig-my-cluster
```

```bash
oc apply -f my-cluster.yaml -n psap-automation
```

The controller automatically:
- Validates the kubeconfig secret
- Discovers GPUs on the target cluster and updates the global `fournos-queue`
  ClusterQueue quotas for this cluster's ResourceFlavor
- Self-heals the lock if the sentinel job is deleted externally

### How cluster locking works

Locking uses the same mechanism as exclusive FournosJobs: **cluster-slot quota**.
Each cluster has 100 `fournos/cluster-slot` quota in the global `fournos-queue`
ClusterQueue. An exclusive job requests all 100 slots, blocking any other job
from being scheduled on that cluster.

When you set `spec.owner` on a PSAPCluster, the controller creates a **sentinel
FournosJob** — a lightweight job with `lockOnly: true` and `exclusive: true`
that requests all 100 cluster-slots without running any pipeline. This holds
the cluster's quota, preventing other jobs from being admitted.

When you clear `spec.owner`, the controller deletes the sentinel job, freeing
the cluster-slots. Any pending jobs are then eligible for admission by Kueue.

### Locking a cluster

```bash
# Lock with a 4-hour TTL (auto-expires after 4 hours)
oc patch psapcluster athena-fire -n psap-automation --type merge \
  -p '{"spec":{"owner":"userA","ttl":"4h"}}'

# Lock indefinitely (must be manually unlocked)
oc patch psapcluster athena-fire -n psap-automation --type merge \
  -p '{"spec":{"owner":"userA"}}'

# Unlock (deletes the sentinel, pending jobs proceed)
oc patch psapcluster athena-fire -n psap-automation --type merge \
  -p '{"spec":{"owner":""}}'
```

#### TTL format

| Format | Example | Duration |
|--------|---------|----------|
| `Nm`   | `30m`   | 30 minutes |
| `Nh`   | `4h`    | 4 hours |
| `Nd`   | `2d`    | 2 days |

If `ttl` is omitted, the lock does not expire and must be cleared manually.

### Common scenarios

#### Scenario 1: UserA needs the cluster for manual work

UserA is running manual experiments on `athena-fire` and wants to make sure
no automated jobs interfere.

```bash
oc patch psapcluster athena-fire -n psap-automation --type merge \
  -p '{"spec":{"owner":"userA","ttl":"4h"}}'
```

**What happens:**
1. The controller creates a sentinel FournosJob (`psapcluster-lock-athena-fire`)
   that holds all 100 cluster-slots on `athena-fire`.
2. Any new jobs targeting `athena-fire` queue up with the message:
   *"Cluster athena-fire is exclusively locked by psapcluster-lock-athena-fire,
   waiting for it to finish"*
3. UserA does his manual work.
4. After 4 hours (or when UserA unlocks early), the sentinel is deleted and
   queued jobs automatically proceed.

#### Scenario 2: Locking while a job is already running

UserB's benchmark job is mid-run on `athena-fire` when UserA locks the cluster.

```bash
oc patch psapcluster athena-fire -n psap-automation --type merge \
  -p '{"spec":{"owner":"userA","ttl":"2h"}}'
```

**What happens:**
1. The sentinel FournosJob is created and goes to **Pending** — it cannot be
   admitted because UserB's job already holds cluster-slots.
2. UserB's job **continues running uninterrupted** until it completes normally.
3. Once UserB's job finishes and releases its cluster-slots, Kueue admits the
   sentinel. UserA now has the lock.
4. Any jobs submitted after step 1 queue behind the sentinel.

There is no preemption — running jobs always finish. The lock takes effect
after currently running work completes.

#### Scenario 3: UserA finishes early

UserA locked the cluster for 4 hours but finished after 1 hour.

```bash
oc patch psapcluster athena-fire -n psap-automation --type merge \
  -p '{"spec":{"owner":""}}'
```

**What happens:**
1. The controller deletes the sentinel FournosJob.
2. Kueue sees 100 cluster-slots freed on `athena-fire`.
3. Any pending jobs are immediately eligible for admission — no need to wait
   for the TTL to expire, no Slack ping at 10:30pm.

#### Scenario 4: UserA forgets to unlock

UserA locked the cluster with `ttl: 4h` and left for the day.

**What happens:**
1. After 4 hours, the reconciler detects the TTL has expired.
2. It automatically clears `spec.owner` and deletes the sentinel FournosJob.
3. Pending jobs proceed as if UserA had unlocked manually.

#### Scenario 5: Submitting a job while a cluster is locked

UserC submits a FournosJob targeting `athena-fire` while UserA has it locked.

```bash
oc create -f my-job.yaml -n psap-automation
```

**What happens:**
1. The job goes through the normal lifecycle: Resolving (Forge) → Pending.
2. During the Pending phase, Kueue cannot admit it because the sentinel holds
   all cluster-slots.
3. The job status shows: *"Cluster athena-fire is exclusively locked by
   psapcluster-lock-athena-fire, waiting for it to finish"*
4. When UserA unlocks (or TTL expires), the sentinel is deleted and UserC's
   job is admitted automatically.

The job's Forge resolution happens normally while it waits — only Kueue
admission is blocked by the lock.

### PSAPCluster spec fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `kubeconfigSecret` | yes | — | Name of the K8s Secret holding the target cluster kubeconfig |
| `owner` | no | — | Person or team claiming exclusive use. Setting this creates a sentinel FournosJob that locks the cluster |
| `ttl` | no | — | Auto-expiry duration (e.g. `4h`, `30m`, `2d`). Lock persists indefinitely if omitted |
| `gpuDiscoveryInterval` | no | `5m` | How often to probe the target cluster for GPU hardware |

### PSAPCluster status fields

| Field | Description |
|-------|-------------|
| `kubeconfigStatus` | `Valid`, `Missing`, `Invalid`, or `Unreachable` |
| `locked` | Whether the cluster is currently locked |
| `lockExpiresAt` | When the lock auto-expires (null if no TTL) |
| `lockJobName` | Name of the sentinel FournosJob holding cluster quota while locked |
| `gpuSummary` | Human-readable GPU summary (e.g. `8x H200`) |
| `hardware.gpus` | Array of `{vendor, model, shortName, count, nodeCount}` |
| `hardware.totalGPUs` | Total GPU count across all types |
| `hardware.lastDiscovery` | Timestamp of last successful GPU discovery |
| `hardware.consecutiveFailures` | Number of consecutive discovery failures |
| `conditions` | `KubeconfigValid`, `GPUDiscovered` |

### Troubleshooting PSAPCluster

**Cluster shows `Unreachable`:**
The controller failed to connect to the target cluster 5 or more times
consecutively. The kubeconfig reconciler will automatically reset the status
to `Valid` once the secret is accessible, and GPU discovery will retry on the
next cycle. Check:
- Is the target cluster up? Try `oc --kubeconfig=<path> cluster-info`
- Is the kubeconfig secret valid? `oc get secret <name> -n psap-secrets -o yaml`
- Check operator logs: `oc logs deployment/fournos -n psap-automation | grep <cluster-name>`

**Cluster shows `Missing` kubeconfig:**
The kubeconfig secret referenced by `spec.kubeconfigSecret` does not exist
in the secrets namespace. Create it with:
```bash
oc create secret generic kubeconfig-<name> \
  --from-file=kubeconfig=/path/to/kubeconfig -n psap-secrets
```

**Sentinel FournosJob stuck in Pending:**
This means another job is currently running on the cluster. The sentinel
queues behind it. Check what's running:
```bash
oc get fournosjobs -n psap-automation -l fournos.dev/exclusive-cluster=<cluster-name>
```
The lock takes effect once the running job finishes.

**Sentinel FournosJob deleted externally:**
The self-healing reconciler (runs every 30s) detects the missing sentinel and
recreates it automatically if `spec.owner` is still set. No manual action needed.

**Lock not expiring:**
- Verify `ttl` is set: `oc get psapcluster <name> -n psap-automation -o jsonpath='{.spec.ttl}'`
- Check `lockExpiresAt`: `oc get psapcluster <name> -n psap-automation -o jsonpath='{.status.lockExpiresAt}'`
- Check operator logs for errors in the TTL reconciler

**GPU count shows 0 or is missing:**
- The target cluster may not have GPU nodes, or GPU device plugins are not installed
- Check node labels: `oc get nodes --show-labels | grep gpu` on the target cluster
- Verify the kubeconfig has permission to list nodes

**Want to see the sentinel job details:**
```bash
oc get fournosjob psapcluster-lock-<cluster-name> -n psap-automation -o yaml
```

### PSAPCluster configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `FOURNOS_PSAPCLUSTER_TIMER_INTERVAL_SEC` | `30` | Reconciliation timer interval |
| `FOURNOS_GPU_DISCOVERY_DEFAULT_INTERVAL_SEC` | `300` | Default GPU discovery interval |
| `FOURNOS_GPU_DISCOVERY_TIMEOUT_SEC` | `10` | Timeout for connecting to target clusters |
| `FOURNOS_CLUSTER_DISCOVERY_INTERVAL_SEC` | `60` | Interval for auto-discovery scan of kubeconfig secrets |

### Testing cluster locking end-to-end

This walkthrough verifies that the sentinel FournosJob mechanism correctly
blocks and unblocks Kueue workloads on a locked cluster.

**1. Lock the cluster and verify the sentinel:**

```bash
# Lock
oc patch psapcluster athena-fire -n psap-automation --type merge \
  -p '{"spec":{"owner":"mehul"}}'

# Confirm sentinel job was created and admitted
oc get fournosjobs -n psap-automation | grep psapcluster-lock
# Expected:
#   psapcluster-lock-athena-fire   mehul   Admitted   athena-fire   Cluster lock held on athena-fire

# Confirm Kueue workload is admitted, holding all 100 cluster-slots
oc get workloads -n psap-automation
# Expected:
#   psapcluster-lock-athena-fire   fournos-queue   fournos-queue   True
```

**2. Submit a competing workload and verify it is blocked:**

```bash
oc create -n psap-automation -f - <<'EOF'
apiVersion: kueue.x-k8s.io/v1beta2
kind: Workload
metadata:
  name: test-lock-workload
  labels:
    kueue.x-k8s.io/queue-name: fournos-queue
spec:
  queueName: fournos-queue
  podSets:
    - name: launcher
      count: 1
      template:
        spec:
          containers:
            - name: placeholder
              image: registry.k8s.io/pause:3.9
              resources:
                requests:
                  fournos/cluster-slot: "100"
          nodeSelector:
            fournos.dev/cluster: athena-fire
          restartPolicy: Never
EOF

# Verify test workload stays pending (not admitted)
oc get workloads -n psap-automation
# Expected:
#   psapcluster-lock-athena-fire   fournos-queue   fournos-queue   True
#   test-lock-workload             fournos-queue                          <-- no ADMITTED

# Describe shows the reason: "insufficient unused quota for fournos/cluster-slot"
oc describe workload test-lock-workload -n psap-automation
```

**3. Unlock the cluster and verify the competing workload is admitted:**

```bash
# Unlock
oc patch psapcluster athena-fire -n psap-automation --type merge \
  -p '{"spec":{"owner":""}}'

# After a few seconds, sentinel is deleted and test workload gets admitted
oc get workloads -n psap-automation
# Expected:
#   test-lock-workload   fournos-queue   fournos-queue   True

# Sentinel job should be gone
oc get fournosjobs -n psap-automation | grep psapcluster-lock
# Expected: no results
```

**4. Clean up:**

```bash
oc delete workload test-lock-workload -n psap-automation
```

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
| `FOURNOS_RESOLVE_IMAGE` | `image-registry.openshift-image-registry.svc:5000/{namespace}/forge-core:main` | Container image for the resolve Job (`{namespace}` is substituted at runtime) |
| `FOURNOS_RESOLVE_DEADLINE_SEC` | `300` | Deadline for the resolve Job (seconds) |
| `FOURNOS_RESOLVE_JOB_TEMPLATE` | `config/forge/resolve_job.yaml` | Path (relative to project root) to the Job YAML template for the resolve step. Override with `dev/mock-resolve/resolve_job.yaml` for local dev/CI. |

## Architecture

```
FournosJob CR ──→ Operator ──→ Forge Resolve Job (patches FournosJob spec) ──→ Kueue Workload ──→ (admission) ──→ Tekton PipelineRun ──→ FORGE ──→ target cluster
```

The operator runs as a single-replica Deployment using
[kopf](https://kopf.dev/). On each `FournosJob`, it:

1. **Resolves** job requirements by launching a Forge K8s Job that populates the FournosJob spec with GPU type/count and secret references
2. **Creates** a Kueue Workload with the resolved GPU resources (owned by the FournosJob via `ownerReferences`)
3. **Polls** (5 s timer) for Kueue admission and assigned cluster
4. **Copies** referenced Vault secrets from the secrets namespace into the operator namespace (per-job copies with `ownerReferences` for automatic cleanup) and **launches** a Tekton PipelineRun with `FJOB_NAME` + `FOURNOS_NAMESPACE` (so FORGE can look up the full FournosJob spec) and the secrets mounted as a projected volume at `/var/run/secrets/fournos/` (owned by the FournosJob via `ownerReferences`)
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

Target clusters need nothing installed — FORGE runs on the hub cluster inside
Tekton Task pods and communicates with targets via `oc`/`kubectl` through
kubeconfig Secrets.

For a detailed breakdown of the CRD, scheduling, operator internals, and key
design decisions, see the [Design Document](Fournos_Design_Document.md).
