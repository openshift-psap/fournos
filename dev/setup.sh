#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME="fournos-dev"

echo "=== Fournos local dev setup ==="

# ---------------------------------------------------------------
# 0. Use Podman as the container runtime for kind
# ---------------------------------------------------------------
export KIND_EXPERIMENTAL_PROVIDER=podman

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
kubectl apply --filename https://storage.googleapis.com/tekton-releases/pipeline/latest/release.yaml
echo "Waiting for Tekton Pipelines to be ready..."
kubectl wait --for=condition=ready pod \
  -l app.kubernetes.io/part-of=tekton-pipelines \
  -n tekton-pipelines --timeout=180s

# ---------------------------------------------------------------
# 3. Install Kueue
# ---------------------------------------------------------------
echo ""
echo "Installing Kueue..."
kubectl apply --server-side -f https://github.com/kubernetes-sigs/kueue/releases/latest/download/manifests.yaml
echo "Waiting for Kueue controller to be ready..."
kubectl wait --for=condition=ready pod \
  -l control-plane=controller-manager \
  -n kueue-system --timeout=180s

# ---------------------------------------------------------------
# 4. Apply Fournos Kubernetes config
# ---------------------------------------------------------------
echo ""
echo "Applying Fournos config..."
kubectl apply -f config/kueue/kueue-config.yaml
kubectl apply -f config/rbac/rbac.yaml
kubectl apply -f config/tekton/

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
