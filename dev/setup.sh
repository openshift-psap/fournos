#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME="${KIND_CLUSTER_NAME:-fournos-dev}"
SECRETS_NAMESPACE="${FOURNOS_SECRETS_NAMESPACE:-psap-secrets}"

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
  echo "Creating kind cluster '${CLUSTER_NAME}' (${KIND_EXPERIMENTAL_PROVIDER} provider)..."
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

#----------------------
# Prepare the namespace
# ---------------------

: "${FOURNOS_WORKLOAD_NAMESPACE:?FOURNOS_WORKLOAD_NAMESPACE must be set}"
# In local dev, controller and execution share the same namespace for simplicity
CONTROLLER_NAMESPACE="${FOURNOS_CONTROLLER_NAMESPACE:-$FOURNOS_WORKLOAD_NAMESPACE}"
export CONTROLLER_NAMESPACE

kubectl create ns "$CONTROLLER_NAMESPACE" --dry-run -oyaml | kubectl apply -f-
if [ "$CONTROLLER_NAMESPACE" != "$FOURNOS_WORKLOAD_NAMESPACE" ]; then
  kubectl create ns "$FOURNOS_WORKLOAD_NAMESPACE" --dry-run -oyaml | kubectl apply -f-
fi
kubectl label ns/$FOURNOS_WORKLOAD_NAMESPACE fournos.dev/queue-access=true
kubectl create ns "$SECRETS_NAMESPACE" --dry-run -oyaml | kubectl apply -f-

# ---------------------------------------------------------------
# 4. Apply FournosJob CRD
# ---------------------------------------------------------------
echo ""
echo "Applying FournosJob CRD..."
kubectl apply -f manifests/crd.yaml -n $FOURNOS_WORKLOAD_NAMESPACE

# ---------------------------------------------------------------
# 5. Apply Fournos Kubernetes manifests
# ---------------------------------------------------------------
echo ""
echo "Applying Fournos manifests..."
kubectl apply -f manifests/rbac/sa_fournos.yaml -n "$CONTROLLER_NAMESPACE"
kubectl apply -f manifests/rbac/sa_fournos.yaml -n "$FOURNOS_WORKLOAD_NAMESPACE"
for rbac_file in manifests/rbac/role_fournos.yaml manifests/rbac/rolebinding_fournos.yaml; do
  cat "$rbac_file" | CONTROLLER_NAMESPACE=$CONTROLLER_NAMESPACE envsubst '$CONTROLLER_NAMESPACE' | kubectl apply -f- -n $FOURNOS_WORKLOAD_NAMESPACE
done
cat manifests/rbac/clusterrole_fournos.yaml | kubectl apply -f-
cat manifests/rbac/clusterrolebinding_fournos.yaml | CONTROLLER_NAMESPACE=$CONTROLLER_NAMESPACE envsubst '$CONTROLLER_NAMESPACE' | kubectl apply -f-
cat manifests/secrets-ns-rbac.yaml \
  | CONTROLLER_NAMESPACE=$CONTROLLER_NAMESPACE SECRETS_NAMESPACE=$SECRETS_NAMESPACE envsubst \
  | kubectl apply -f-

# ---------------------------------------------------------------
# 6. Apply mock resources (overrides real Tasks, adds fake secrets, kueues ...)
# ---------------------------------------------------------------
echo ""
echo "Applying mock resources..."
kubectl apply -f dev/mock-kueue-config.yaml -n $FOURNOS_WORKLOAD_NAMESPACE
kubectl apply -f dev/mock-pipelines -n $FOURNOS_WORKLOAD_NAMESPACE
kubectl apply -f dev/mock-secrets.yaml -n $SECRETS_NAMESPACE

# ---------------------------------------------------------------
# 7. Build and load mock resolve image
# ---------------------------------------------------------------
echo ""
MOCK_RESOLVE_IMAGE="fournos-mock-resolve:dev"
CONTAINER_RUNTIME="${KIND_EXPERIMENTAL_PROVIDER:-podman}"
echo "Building mock resolve image ($CONTAINER_RUNTIME)..."
"$CONTAINER_RUNTIME" build -t "$MOCK_RESOLVE_IMAGE" dev/mock-resolve/
echo "Loading mock resolve image into kind..."
ARCHIVE=$(mktemp /tmp/fournos-mock-resolve-XXXXXX.tar)
if [ "$CONTAINER_RUNTIME" = "podman" ]; then
  # Podman stores images as localhost/<name>; containerd resolves bare
  # names to docker.io/library/<name>.  Re-tag so the archive carries
  # the name containerd will look up.
  "$CONTAINER_RUNTIME" tag "$MOCK_RESOLVE_IMAGE" "docker.io/library/$MOCK_RESOLVE_IMAGE"
  "$CONTAINER_RUNTIME" save -o "$ARCHIVE" "docker.io/library/$MOCK_RESOLVE_IMAGE"
else
  "$CONTAINER_RUNTIME" save -o "$ARCHIVE" "$MOCK_RESOLVE_IMAGE"
fi
kind load image-archive "$ARCHIVE" --name "${CLUSTER_NAME}"
rm -f "$ARCHIVE"

# ---------------------------------------------------------------
# 8. Build and load mock generic pipeline images
# ---------------------------------------------------------------
echo ""
echo "Building mock generic runner image ($CONTAINER_RUNTIME)..."
MOCK_GENERIC_RUNNER_IMAGE="fournos-mock-generic-runner:dev"
"$CONTAINER_RUNTIME" build -t "$MOCK_GENERIC_RUNNER_IMAGE" -f dev/mock-generic/Dockerfile.runner dev/mock-generic/

echo "Building mock generic user image ($CONTAINER_RUNTIME)..."
MOCK_GENERIC_USER_IMAGE="fournos-mock-generic-user:dev"
"$CONTAINER_RUNTIME" build -t "$MOCK_GENERIC_USER_IMAGE" dev/mock-generic/

echo "Loading mock generic images into kind..."
for IMG_NAME in "$MOCK_GENERIC_RUNNER_IMAGE" "$MOCK_GENERIC_USER_IMAGE"; do
  ARCHIVE=$(mktemp /tmp/fournos-mock-generic-XXXXXX.tar)
  if [ "$CONTAINER_RUNTIME" = "podman" ]; then
    "$CONTAINER_RUNTIME" tag "$IMG_NAME" "docker.io/library/$IMG_NAME"
    "$CONTAINER_RUNTIME" save -o "$ARCHIVE" "docker.io/library/$IMG_NAME"
  else
    "$CONTAINER_RUNTIME" save -o "$ARCHIVE" "$IMG_NAME"
  fi
  kind load image-archive "$ARCHIVE" --name "${CLUSTER_NAME}"
  rm -f "$ARCHIVE"
done

# ---------------------------------------------------------------
# 9. Apply fournos-generic Pipeline and Task for local dev
# ---------------------------------------------------------------
echo ""
echo "Applying fournos-generic Pipeline and Task..."
# containerd inside kind resolves bare image names to docker.io/library/
MOCK_GENERIC_RUNNER_IMAGE_FQDN="docker.io/library/$MOCK_GENERIC_RUNNER_IMAGE"
cat <<EOF | kubectl apply -n $FOURNOS_WORKLOAD_NAMESPACE -f-
apiVersion: tekton.dev/v1
kind: Task
metadata:
  name: fournos-generic-step
spec:
  params:
    - name: fjob-name
      type: string
    - name: fournos-workload-namespace
      type: string
    - name: kubeconfig-secret
      type: string
  workspaces:
    - name: artifacts
  steps:
    - name: run
      image: $MOCK_GENERIC_RUNNER_IMAGE_FQDN
      # Explicit command prevents Tekton from resolving the image
      # entrypoint against the registry (which fails for local images).
      command: ["python3", "/opt/fournos/entrypoint"]
      imagePullPolicy: IfNotPresent
      env:
        - name: FJOB_NAME
          value: "\$(params.fjob-name)"
        - name: FOURNOS_WORKLOAD_NAMESPACE
          value: "\$(params.fournos-workload-namespace)"
        - name: FOURNOS_STEP
          value: run
        - name: ARTIFACT_DIR
          value: "\$(workspaces.artifacts.path)"
      volumeMounts:
        - name: kubeconfig
          mountPath: /secrets/kubeconfig
          readOnly: true
  volumes:
    - name: kubeconfig
      secret:
        secretName: "\$(params.kubeconfig-secret)"
        optional: true
---
apiVersion: tekton.dev/v1
kind: Pipeline
metadata:
  name: fournos-generic
  annotations:
    fournos.dev/resolve-image: "$MOCK_GENERIC_RUNNER_IMAGE_FQDN"
spec:
  params:
    - name: fjob-name
      type: string
    - name: fournos-workload-namespace
      type: string
    - name: kubeconfig-secret
      type: string
  workspaces:
    - name: artifacts
  tasks:
    - name: run-generic-job
      taskRef:
        name: fournos-generic-step
      params:
        - name: fjob-name
          value: "\$(params.fjob-name)"
        - name: fournos-workload-namespace
          value: "\$(params.fournos-workload-namespace)"
        - name: kubeconfig-secret
          value: "\$(params.kubeconfig-secret)"
      workspaces:
        - name: artifacts
          workspace: artifacts
EOF

cat config/generic/rbac.yaml | NAMESPACE=$FOURNOS_WORKLOAD_NAMESPACE envsubst '$NAMESPACE' | kubectl apply -f-

# ---------------------------------------------------------------
# Done
# ---------------------------------------------------------------
echo ""
echo "============================================"
echo "  Dev cluster ready!"
echo ""
echo "  Start operator:  make dev-run"
echo "  Run tests:       make test"
echo "  Tear down:       make dev-teardown"
echo "============================================"
