# FournosCluster Controller

A Kubernetes operator that manages GPU cluster lifecycle for the Fournos job scheduler. It watches kubeconfig secrets, auto-discovers clusters, discovers GPUs, and implements cluster locking via Kueue.

## Prerequisites

- `oc` CLI authenticated to the management cluster (psap-automation)
- `podman` for building container images
- Python 3.12+ with a virtual environment for local development
- Access to `quay.io/memehta/fournos-cluster` image repository

## Quick Start

```bash
# 1. Build (from Mac, target linux/amd64 for OpenShift)
cd fournos-cluster
podman build --platform linux/amd64 -t quay.io/memehta/fournos-cluster:latest -f Containerfile .

# 2. Push
podman push quay.io/memehta/fournos-cluster:latest

# 3. Deploy
export KUBECONFIG=/Users/memehta/kubeconfigs/psap-automation-kubeconfig
oc apply -f manifests/crd.yaml
oc apply -f manifests/rbac/role.yaml
oc apply -f deploy/deployment.yaml

# 4. Verify
oc get pods -n psap-automation -l app=fournos-cluster
oc logs -l app=fournos-cluster -n psap-automation -f
```

## Architecture

The controller runs as a single-replica Deployment in `psap-automation` and watches two namespaces:

| Namespace | What it watches |
|-----------|-----------------|
| `psap-secrets` | Secrets labeled `fournos.dev/cluster-kubeconfig=true` |
| `psap-automation` | FournosCluster custom resources |

When a labeled kubeconfig secret is detected, the controller:

1. Creates a `FournosCluster` CR in `psap-automation`
2. Validates the kubeconfig by reading the secret
3. Discovers GPUs on the target cluster via the Kubernetes API
4. Creates a Kueue `ResourceFlavor` and adds it to the `ClusterQueue`

## Deployment (ad-hoc deployment at the moment)

NOTE:
This is an ad-hoc deployment at the moment. It will be replaced by the gitops deployment process.

### Step 1: Build the container image

Always use `--platform linux/amd64` when building on Mac (ARM) for OpenShift (x86):

```bash
cd fournos-cluster
podman build --platform linux/amd64 -t quay.io/memehta/fournos-cluster:latest -f Containerfile .
```

### Step 2: Push to registry

```bash
podman push quay.io/memehta/fournos-cluster:latest
```


The quay.io repository must be public (or have a pull secret configured on the cluster).

### Step 3: Apply CRD

```bash
oc apply -f manifests/crd.yaml
```

This creates the `FournosCluster` custom resource definition (`fournoscluster.fournos.dev`).

### Step 4: Apply RBAC

```bash
oc apply -f manifests/rbac/role.yaml
```

This creates a `ClusterRole` named `fournos-cluster` with permissions for:
- FournosCluster CRDs (create, watch, patch)
- Secrets (get, list, watch, patch — for kubeconfig access and kopf annotations)
- FournosJobs (create, get, delete — for sentinel lock jobs)
- Kueue ResourceFlavors and ClusterQueues (for GPU quota management)
- CRD discovery and namespace observation (required by kopf)

The `ClusterRoleBinding` is included in `deploy/deployment.yaml`.

### Step 5: Apply Deployment

```bash
oc apply -f deploy/deployment.yaml
```

This creates:
- `ServiceAccount` named `fournos-cluster`
- `ClusterRoleBinding` binding the SA to the `fournos-cluster` ClusterRole
- `Deployment` with liveness probe on `/healthz:8080`

### Step 6: Verify

```bash
# Pod should be Running with 0 restarts
oc get pods -n psap-automation -l app=fournos-cluster

# Logs should show "FournosCluster controller started"
oc logs -l app=fournos-cluster -n psap-automation --tail=20

# After labeling a secret, a FournosCluster CR should appear
oc get fournoscluster -n psap-automation
```

## Testing

### Test 1: Auto-discovery via labeled secrets

Label a kubeconfig secret to trigger auto-discovery:

```bash
# Label an existing kubeconfig secret
oc label secret kubeconfig-athena-fire fournos.dev/cluster-kubeconfig=true -n psap-secrets

# Verify FournosCluster CR is created
oc get fournoscluster -n psap-automation

# Expected output:
# NAME          OWNER   GPUS       KUBECONFIG   LOCKED   AGE
# athena-fire                      Valid         false    5s
```

Check the controller logs for confirmation:

```bash
oc logs -l app=fournos-cluster -n psap-automation --tail=20
# Should show: "FournosCluster athena-fire: initialized (kubeconfig=Valid)"
```

### Test 2: GPU discovery

After the CR is created, the controller auto-discovers GPUs on the target cluster:

```bash
oc get fournoscluster athena-fire -n psap-automation -o jsonpath='{.status.gpuSummary}'
# Expected: "8x NVIDIA" (or similar, depending on the cluster)

oc get fournoscluster athena-fire -n psap-automation -o jsonpath='{.spec.hardware.gpus}' | python3 -m json.tool
# Shows detailed GPU info: vendor, model, shortName, count, nodeCount
```

GPU discovery runs periodically (default: every 5 minutes). Failed discoveries use exponential backoff.

### Test 3: Cluster locking

Lock a cluster by setting the `owner` field:

```bash
oc patch fournoscluster athena-fire -n psap-automation \
  --type=merge -p '{"spec":{"owner":"your-name"}}'
```

Verify the lock:

```bash
# FournosCluster should show locked=true
oc get fournoscluster athena-fire -n psap-automation -o jsonpath='{.status.locked}'
# Expected: true

# A sentinel FournosJob should exist
oc get fournosjobs -n psap-automation
# Expected:
# NAME                       OWNER       PHASE      CLUSTER       MESSAGE                             AGE
# cluster-lock-athena-fire   your-name   Admitted   athena-fire   Cluster lock held on athena-fire    10s

# The sentinel's Kueue Workload should be admitted, consuming all 100 cluster-slots
oc get workloads -n psap-automation
# Expected:
# NAME                       QUEUE           RESERVED IN     ADMITTED   AGE
# cluster-lock-athena-fire   fournos-queue   fournos-queue   True       10s
```

### Test 4: Verify lock blocks new jobs

While the lock is held, create a test Kueue Workload targeting the same cluster:

```bash
cat <<'EOF' | oc create -n psap-automation -f -
apiVersion: kueue.x-k8s.io/v1beta2
kind: Workload
metadata:
  name: test-blocked-job
  labels:
    kueue.x-k8s.io/queue-name: fournos-queue
spec:
  active: true
  queueName: fournos-queue
  podSets:
    - name: launcher
      count: 1
      template:
        spec:
          restartPolicy: Never
          containers:
            - name: placeholder
              image: registry.k8s.io/pause:3.9
              resources:
                requests:
                  fournos/cluster-slot: "1"
          nodeSelector:
            fournos.dev/cluster: athena-fire
EOF

# Verify it is NOT admitted (no RESERVED IN, no ADMITTED)
oc get workloads -n psap-automation
# Expected:
# NAME                       QUEUE           RESERVED IN     ADMITTED   AGE
# cluster-lock-athena-fire   fournos-queue   fournos-queue   True       2m
# test-blocked-job           fournos-queue                              5s

# Check the pending reason
oc get workload test-blocked-job -n psap-automation \
  -o jsonpath='{.status.conditions[*].message}'
# Expected: "insufficient unused quota for fournos/cluster-slot in flavor athena-fire"
```

### Test 5: Lock release

Release the lock by clearing the owner:

```bash
oc patch fournoscluster athena-fire -n psap-automation \
  --type=merge -p '{"spec":{"owner":""}}'

# Verify lock is released
oc get fournoscluster athena-fire -n psap-automation -o jsonpath='{.status.locked}'
# Expected: false

# Sentinel FournosJob should be deleted
oc get fournosjobs -n psap-automation
# Expected: No resources found

# The previously blocked workload should now be ADMITTED
oc get workloads -n psap-automation
# Expected:
# NAME               QUEUE           RESERVED IN     ADMITTED   AGE
# test-blocked-job   fournos-queue   fournos-queue   True       30s
```

Clean up the test workload:

```bash
oc delete workload test-blocked-job -n psap-automation
```

### Test 6: Lock with TTL

Set a lock with an automatic expiry:

```bash
oc patch fournoscluster athena-fire -n psap-automation \
  --type=merge -p '{"spec":{"owner":"your-name","ttl":"30m"}}'

# Check the expiry time
oc get fournoscluster athena-fire -n psap-automation \
  -o jsonpath='{.status.lockExpiresAt}'
# Expected: ISO timestamp 30 minutes from now

# The lock will automatically release when the TTL expires
# (checked every reconcile interval, default 60s)
```

## Troubleshooting

### Pod in CrashLoopBackOff

Check logs for RBAC errors:

```bash
oc logs -l app=fournos-cluster -n psap-automation --tail=50
```

Common causes:
- **"cannot list resource" errors**: ClusterRole is missing permissions. Re-apply `manifests/rbac/role.yaml`.
- **"Forbidden" on secrets**: The controller needs `get`, `list`, `watch`, `patch` on secrets across both namespaces.

### No FournosCluster CR created after labeling secret

1. Verify the label is correct:
   ```bash
   oc get secret <name> -n psap-secrets --show-labels | grep fournos.dev/cluster-kubeconfig
   ```

2. Check controller logs for errors:
   ```bash
   oc logs -l app=fournos-cluster -n psap-automation --tail=30
   ```

3. Verify the secret has a `kubeconfig` key:
   ```bash
   oc get secret <name> -n psap-secrets -o jsonpath='{.data}' | python3 -m json.tool | grep kubeconfig
   ```

### GPU discovery shows 0 GPUs

- The target cluster may not have GPU Feature Discovery (GFD) installed. Without GFD, the `nvidia.com/gpu.product` label is missing on nodes.
- Even without GFD, GPUs are still detected via `nvidia.com/gpu` allocatable resources — they show as `8x NVIDIA` instead of `8x A100`.
- Check discovery errors: `oc get fournoscluster <name> -n psap-automation -o jsonpath='{.spec.hardware.lastError}'`

### Sentinel FournosJob stuck in Pending

The sentinel needs Kueue to admit its Workload. Check:

```bash
# Is there another workload consuming the cluster-slots?
oc get workloads -n psap-automation

# Delete stale workloads if needed
oc delete workload <stale-workload-name> -n psap-automation
```

## Configuration

Environment variables (set in `deploy/deployment.yaml`):

| Variable | Default | Description |
|----------|---------|-------------|
| `FOURNOS_CLUSTER_NAMESPACE` | `psap-automation` | Namespace for FournosCluster CRs |
| `FOURNOS_CLUSTER_SECRETS_NAMESPACE` | `psap-secrets` | Namespace containing kubeconfig secrets |

## Local Development

```bash
# Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate

# Install in dev mode
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Run with coverage
pytest tests/ -v --cov=fournos_cluster --cov-report=term-missing
```
