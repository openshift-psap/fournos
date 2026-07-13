# Adding New Secrets to the Vault

This guide describes how to create and register new secrets in the OpenShift CI Vault for use in fournos CI jobs.

## Prerequisites

- Access to the PSAP management cluster
- An account on [vault.ci.openshift.org](https://vault.ci.openshift.org/ui/)
- The fournos repo checked out with a Python virtualenv set up

Reference: [OpenShift CI docs — Adding a New Secret to CI](https://docs.ci.openshift.org/how-tos/adding-a-new-secret-to-ci/)

## Steps

### 1. Create the secret in Vault

1. Go to [vault.ci.openshift.org/ui/](https://vault.ci.openshift.org/ui/) and log in.
2. Navigate to `selfservice/psap/` and create a new secret or directory there.

### 2. Copy your Vault token

After logging in, click the user icon in the **top-left corner** and select **Copy token**.

```
export VAULT_TOKEN=hvs...
```

### 3. Set up the Python environment

From the fournos repo root:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 4. Sync the secrets

Run the sync script to push the new secret into the cluster:

```bash
./hacks/sync_vault_secrets.py
```

> **Note:** You must be connected to the PSAP management cluster for the sync to succeed.
