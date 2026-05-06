#!/bin/bash

set -o pipefail
set -o errexit
set -o nounset
set -o errtrace

# Configuration
NAMESPACE="fournos-controller"
SERVICE_ACCOUNT="ocpci"
SECRET_NAME="ocpci-token"
CONTEXT=$(kubectl config current-context)
CLUSTER_NAME=$(kubectl config view -o jsonpath="{.contexts[?(@.name==\"$CONTEXT\")].context.cluster}")
SERVER=$(kubectl config view -o jsonpath="{.clusters[?(@.name==\"$CLUSTER_NAME\")].cluster.server}")
OUTPUT_FILE="kubeconfig-ocpci.yaml"

# Wait for the secret to be populated with the token
echo "Waiting for secret $SECRET_NAME to be populated..."
until kubectl get secret "$SECRET_NAME" -n "$NAMESPACE" -o jsonpath='{.data.token}' &> /dev/null; do
    sleep 1
done

# Get the token
TOKEN=$(kubectl get secret "$SECRET_NAME" -n "$NAMESPACE" -o jsonpath='{.data.token}' | base64 --decode)

# Get the CA certificate
CA_CERT=$(kubectl get secret "$SECRET_NAME" -n "$NAMESPACE" -o jsonpath='{.data.ca\.crt}')

# Create the kubeconfig
cat <<EOF > "$OUTPUT_FILE"
apiVersion: v1
kind: Config
clusters:
- name: default-cluster
  cluster:
    certificate-authority-data: $CA_CERT
    server: $SERVER
contexts:
- name: default-context
  context:
    cluster: default-cluster
    namespace: $NAMESPACE
    user: $SERVICE_ACCOUNT
current-context: default-context
users:
- name: $SERVICE_ACCOUNT
  user:
    token: $TOKEN
EOF

echo "Kubeconfig generated at $OUTPUT_FILE"
