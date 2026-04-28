#!/usr/bin/env bash
# Mock Forge resolve script — patches the FournosJob spec with resolved values.
#
# In production, Forge determines hardware requirements and secret
# references by inspecting the project.  This mock sets hard-coded
# defaults for hardware only when the user hasn't provided them, and
# always sets secretRefs.
#
# Expected env vars (set by the operator):
#   FOURNOS_JOB_NAME    — FournosJob name to patch
#   FOURNOS_NAMESPACE   — target namespace
#   FORGE_PROJECT       — Forge project name
set -euo pipefail

echo "[mock-resolve] job=${FOURNOS_JOB_NAME}"
echo "[mock-resolve] project=${FORGE_PROJECT}"

EXISTING_HW=$(kubectl get fournosjob "${FOURNOS_JOB_NAME}" \
  -n "${FOURNOS_NAMESPACE}" \
  -o jsonpath='{.spec.hardware.gpuType}' 2>/dev/null || true)

if [[ -z "${EXISTING_HW}" ]]; then
  echo "[mock-resolve] no user-provided hardware, setting defaults"
  kubectl patch fournosjob "${FOURNOS_JOB_NAME}" \
    -n "${FOURNOS_NAMESPACE}" \
    --type=merge \
    -p '{"spec":{"hardware":{"gpuType":"a100","gpuCount":2}}}'
else
  echo "[mock-resolve] user-provided hardware found (${EXISTING_HW}), keeping"
fi

echo "[mock-resolve] setting secretRefs"
kubectl patch fournosjob "${FOURNOS_JOB_NAME}" \
  -n "${FOURNOS_NAMESPACE}" \
  --type=merge \
  -p '{"spec":{"secretRefs":["vault-placeholder"]}}'

echo "[mock-resolve] done"
