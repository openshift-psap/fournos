#!/bin/bash

set -o pipefail
set -o errexit
set -o nounset
set -o errtrace

# Use the first argument if provided, otherwise use the current project name
TARGET_NS=${1:-$(oc project -q)}
SOURCE_NS="psap-automation"

echo "Copying kubeconfig-* secrets from $SOURCE_NS to $TARGET_NS..."

# Loop through filtered secrets
for SECRET_NAME in $(oc get secrets -n $SOURCE_NS -o name | grep "kubeconfig-"); do
    # Extract from source and create in target
    # 1. Fetch the secret in JSON
    # 2. Use jq to delete all the "junk" metadata
    # 3. Create it in the new namespace
    oc get "$SECRET_NAME" -n "$SOURCE_NS" -o json | jq '
        del(
            .metadata.namespace, 
            .metadata.uid, 
            .metadata.resourceVersion, 
            .metadata.creationTimestamp, 
            .metadata.managedFields,
            .metadata.ownerReferences
        )' | oc apply -n "$TARGET_NS" -f-
done
