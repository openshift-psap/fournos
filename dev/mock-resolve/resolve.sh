#!/usr/bin/env bash
# Mock Forge resolve script — patches the FournosJob spec with resolved values.
#
# In production, Forge determines hardware requirements and secret
# references by inspecting the project.  This mock always sets the same
# hard-coded defaults.
#
# Expected env vars (set by the operator):
#   FOURNOS_JOB_NAME    — FournosJob name to patch
#   FOURNOS_NAMESPACE   — target namespace
#   FORGE_PROJECT       — Forge project name
set -euo pipefail

echo "[mock-resolve] job=${FOURNOS_JOB_NAME}"
echo "[mock-resolve] project=${FORGE_PROJECT}"

kubectl patch fournosjob "${FOURNOS_JOB_NAME}" \
  -n "${FOURNOS_NAMESPACE}" \
  --type=merge \
  -p '{
    "spec": {
      "hardware": {
        "gpuType": "a100",
        "gpuCount": 2
      },
      "secretRefs": ["vault-placeholder"]
    }
  }'

echo "[mock-resolve] done"
