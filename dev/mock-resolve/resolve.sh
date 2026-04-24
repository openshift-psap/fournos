#!/usr/bin/env bash
# Mock Forge resolve script — patches an existing FournosJobConfig with fixed values.
#
# The FournosJobConfig is pre-created by the Fournos operator with proper
# metadata (labels, ownerReferences).  Forge's job is to populate the spec.
#
# Expected env vars (set by the operator):
#   FOURNOS_JOB_NAME    — parent FournosJob name
#   FOURNOS_NAMESPACE   — target namespace
#   FOURNOS_CONFIG_NAME — name of the FournosJobConfig CR to patch
#   FORGE_PROJECT       — Forge project name
set -euo pipefail

echo "[mock-resolve] job=${FOURNOS_JOB_NAME}"
echo "[mock-resolve] project=${FORGE_PROJECT}"
echo "[mock-resolve] patching FournosJobConfig ${FOURNOS_CONFIG_NAME}"

kubectl patch fournosjobconfig "${FOURNOS_CONFIG_NAME}" \
  -n "${FOURNOS_NAMESPACE}" \
  --type=merge \
  -p '{
    "spec": {
      "hardware": {
        "gpuType": "a100",
        "gpuCount": 2
      },
      "secretRefs": []
    }
  }'

echo "[mock-resolve] done"
