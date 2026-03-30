#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME="${KIND_CLUSTER_NAME:-fournos-dev}"

echo "=== Fournos local dev setup ==="

# ---------------------------------------------------------------
# 0. Container runtime for kind (default: podman, override with
#    KIND_EXPERIMENTAL_PROVIDER=docker for CI / Docker-based envs)
# ---------------------------------------------------------------
export KIND_EXPERIMENTAL_PROVIDER="${KIND_EXPERIMENTAL_PROVIDER:-podman}"

# ---------------------------------------------------------------
# 1. kind cluster
# ---------------------------------------------------------------
if kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
  echo "kind cluster '${CLUSTER_NAME}' already exists, reusing."
else
  echo "Creating kind cluster '${CLUSTER_NAME}' (podman provider)..."
  kind create cluster --name "${CLUSTER_NAME}" --wait 60s
fi

kubectl cluster-info --context "kind-${CLUSTER_NAME}"

# ---------------------------------------------------------------
# 2. Install Tekton Pipelines
# ---------------------------------------------------------------
echo ""
echo "Installing Tekton Pipelines..."
kubectl apply --filename https://infra.tekton.dev/tekton-releases/pipeline/previous/v1.9.2/release.yaml
echo "Waiting for Tekton Pipelines to be ready..."
kubectl wait --for=condition=ready pod \
  -l app.kubernetes.io/part-of=tekton-pipelines \
  -n tekton-pipelines --timeout=180s

# ---------------------------------------------------------------
# 3. Install Kueue
# ---------------------------------------------------------------
echo ""
echo "Installing Kueue..."
kubectl apply --server-side -f https://github.com/kubernetes-sigs/kueue/releases/download/v0.16.4/manifests.yaml
echo "Waiting for Kueue controller to be ready..."
kubectl wait --for=condition=ready pod \
  -l control-plane=controller-manager \
  -n kueue-system --timeout=180s

echo "Waiting for Kueue webhook to be reachable (timeout 60s)..."
SECONDS=0
until kubectl create -f - --dry-run=server -o yaml &>/dev/null <<'EOF'
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: webhook-probe
EOF
do
  if (( SECONDS >= 60 )); then
    echo "ERROR: Kueue webhook not reachable after 60s" >&2
    exit 1
  fi
  sleep 2
done

# ---------------------------------------------------------------
# 4. Apply Fournos Kubernetes manifests
# ---------------------------------------------------------------
echo ""
echo "Applying Fournos manifests..."
kubectl apply -f manifests/kueue-config.yaml
kubectl apply -f manifests/rbac.yaml
kubectl apply -f manifests/tekton/

# ---------------------------------------------------------------
# 5. Apply mock resources (overrides real Tasks, adds fake secrets)
# ---------------------------------------------------------------
echo ""
echo "Applying mock resources..."
kubectl apply -f dev/mock-resources.yaml

# ---------------------------------------------------------------
# Done
# ---------------------------------------------------------------
echo ""
echo "============================================"
echo "  Dev cluster ready!"
echo ""
echo "  Start Fournos:  make dev-run"
echo "  Run tests:      make dev-test"
echo "  Tear down:      make dev-teardown"
echo "============================================"
