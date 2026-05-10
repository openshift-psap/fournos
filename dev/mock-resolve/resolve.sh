#!/usr/bin/env bash
# Mock Forge resolve script — patches the FournosJob spec with resolved values.
#
# In production, Forge determines hardware requirements and secret
# references by inspecting the project.  This mock sets hard-coded
# defaults for hardware only when the user hasn't provided them, and
# always sets secretRefs.
#
# Expected env vars (set by the operator):
#   FJOB_NAME    — FournosJob name to patch
#   FOURNOS_WORKLOAD_NAMESPACE   — namespace where FournosJobs live
set -euo pipefail

echo "[mock-resolve] job=${FJOB_NAME}"
echo "[mock-resolve] namespace=${FOURNOS_WORKLOAD_NAMESPACE}"

EXISTING_HW=$(kubectl get fournosjob "${FJOB_NAME}" \
  -n "${FOURNOS_WORKLOAD_NAMESPACE}" \
  -o jsonpath='{.spec.hardware.gpuType}' 2>/dev/null || true)

if [[ -z "${EXISTING_HW}" ]]; then
  echo "[mock-resolve] no user-provided hardware, setting defaults"
  kubectl patch fournosjob "${FJOB_NAME}" \
    -n "${FOURNOS_WORKLOAD_NAMESPACE}" \
    --type=merge \
    -p '{"spec":{"hardware":{"gpuType":"a100","gpuCount":2}}}'
else
  echo "[mock-resolve] user-provided hardware found (${EXISTING_HW}), keeping"
fi

echo "[mock-resolve] setting secretRefs"
kubectl patch fournosjob "${FJOB_NAME}" \
  -n "${FOURNOS_WORKLOAD_NAMESPACE}" \
  --type=merge \
  -p '{"spec":{"secretRefs":["placeholder"]}}'

echo "[mock-resolve] done"
