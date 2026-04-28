#!/usr/bin/env bash
# Mock Forge resolve script — patches the FournosJob spec with resolved values.
#
# Forge writes hardware (only when not user-provided) and secretRefs
# directly into the FournosJob spec.  This mock preserves any
# user-provided values (hardware, secretRefs) and only fills in defaults
# for fields that are missing.
#
# Expected env vars (set by the operator):
#   FOURNOS_JOB_NAME    — FournosJob name to patch
#   FOURNOS_NAMESPACE   — target namespace
#   FORGE_PROJECT       — Forge project name
set -euo pipefail

echo "[mock-resolve] job=${FOURNOS_JOB_NAME}"
echo "[mock-resolve] project=${FORGE_PROJECT}"

SPEC_JSON=$(kubectl get fournosjob "${FOURNOS_JOB_NAME}" \
  -n "${FOURNOS_NAMESPACE}" \
  -o jsonpath='{.spec}')

EXISTING_HW=$(echo "$SPEC_JSON" | grep -o '"gpuType":"[^"]*"' | head -1 | cut -d'"' -f4 || true)
EXISTING_REFS=$(kubectl get fournosjob "${FOURNOS_JOB_NAME}" \
  -n "${FOURNOS_NAMESPACE}" \
  -o 'jsonpath={.spec.secretRefs[*]}' 2>/dev/null || true)

# Build the patch — only set fields that Forge would resolve.
HW_PATCH=""
if [[ -z "${EXISTING_HW}" ]]; then
  echo "[mock-resolve] no user-provided hardware, setting defaults"
  HW_PATCH='"hardware":{"gpuType":"a100","gpuCount":2}'
else
  echo "[mock-resolve] user-provided hardware found (${EXISTING_HW}), keeping"
fi

REFS_PATCH=""
if [[ -z "${EXISTING_REFS}" ]]; then
  echo "[mock-resolve] no user-provided secretRefs, setting empty"
  REFS_PATCH='"secretRefs":[]'
else
  echo "[mock-resolve] user-provided secretRefs found, preserving"
fi

# Assemble a minimal merge-patch from non-empty fragments.
FRAGMENTS=()
[[ -n "$HW_PATCH" ]]   && FRAGMENTS+=("$HW_PATCH")
[[ -n "$REFS_PATCH" ]] && FRAGMENTS+=("$REFS_PATCH")

if [[ ${#FRAGMENTS[@]} -gt 0 ]]; then
  IFS=,; BODY="${FRAGMENTS[*]}"; unset IFS
  kubectl patch fournosjob "${FOURNOS_JOB_NAME}" \
    -n "${FOURNOS_NAMESPACE}" \
    --type=merge \
    -p "{\"spec\":{${BODY}}}"
else
  echo "[mock-resolve] nothing to patch"
fi

echo "[mock-resolve] done"
