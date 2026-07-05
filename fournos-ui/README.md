# Fournos Dashboard

A web dashboard for managing [Fournos](https://github.com/openshift-psap/fournos-operator) performance testing jobs on Kubernetes. Submit jobs, monitor live runs, schedule recurring tests, and review historical results -- all from one place.

## What It Does

- **Live job monitoring** -- Watch running FournosJobs with real-time log streaming (SSE) and pipeline progress tracking.
- **Job submission** -- Submit new FournosJobs with project, preset, cluster, and config override selection.
- **Scheduling** -- Create Kubernetes CronJobs for recurring test runs, with optional version-resolver scripts that dynamically determine parameters at runtime.
- **History** -- Browse completed jobs stored in PostgreSQL with status, duration, and direct links to MLflow artifacts.
- **Schedule tracking** -- See which schedule triggered each job (manual vs. scheduled) and view all runs for a given schedule.

## Architecture

```
┌─────────────┐     ┌──────────────────┐     ┌────────────┐
│   Browser    │────▶│  FastAPI + HTMX  │────▶│ Kubernetes │
│              │◀────│   (Dashboard)    │◀────│    API     │
└─────────────┘     └────────┬─────────┘     └────────────┘
                             │
                    ┌────────▼─────────┐
                    │   PostgreSQL     │
                    │  (job history)   │
                    └──────────────────┘
```

- **FastAPI** backend with **Jinja2** templates and **HTMX** for dynamic updates.
- **Kubernetes Python client** for watching FournosJob CRs, streaming pod logs, and managing CronJobs.
- **PostgreSQL** (via SQLAlchemy async + asyncpg) for persisting job metadata and schedule tracking.
- A background **watcher thread** monitors FournosJob events and archives them to PostgreSQL automatically.

## Prerequisites

- A Kubernetes / OpenShift cluster with the [Fournos Operator](https://github.com/openshift-psap/fournos-operator) installed.
- A container registry to push the dashboard image.
- `kubectl` or `oc` CLI configured with cluster access.

## Getting Started

### 1. Clone and configure the overlay

```bash
cd kustomize/overlays/ocp/

# Copy example files
cp kustomization.yaml.example kustomization.yaml
cp projects.yaml.example projects.yaml
cp params.env.example params.env
cp ../../base/postgresql-secret.env.example postgresql-secret.env
```

Edit each file with your values:
- **`kustomization.yaml`** -- Set your dashboard image, PostgreSQL image, target namespace, and storage class.
- **`projects.yaml`** -- Define your Forge projects, clusters, and presets.
- **`postgresql-secret.env`** -- Set your database credentials.
- **`params.env`** -- Set your storage class and size.

### 2. Build and push the dashboard image

### 3. Deploy to the cluster

```bash
cd kustomize/overlays/ocp/

# Apply the main stack
oc kustomize . | oc apply -f -

# Apply the cross-namespace RoleBinding (grants dashboard access to the jobs namespace)
oc apply -f rolebinding-psap-automation.yaml
```

This creates:
- A `fournos-dashboard` namespace
- PostgreSQL StatefulSet with persistent storage
- Dashboard Deployment, Service, ServiceAccount
- ClusterRole for FournosJob/CronJob/Pod access
- RoleBinding in the target namespace (e.g. `psap-automation`)
- Projects ConfigMap

### 4. Access the dashboard

```bash
oc port-forward -n fournos-dashboard svc/fournos-dashboard 8000:8000
```

Open http://localhost:8000


## Configuration

All configuration is via environment variables (set in the deployment manifest):

| Variable | Description | Default |
|---|---|---|
| `DATABASE_URL` | PostgreSQL connection string (required) | *none -- must be set* |
| `FOURNOS_NAMESPACE` | Namespace where FournosJobs run | *set via overlay* |
| `PROJECTS_CONFIG_PATH` | Path to projects YAML | `/etc/fournos-dashboard/projects.yaml` |
| `K8S_REQUEST_TIMEOUT` | Timeout for K8s API calls (seconds) | `30` |
| `LOG_LEVEL` | Logging level | `INFO` |
| `KUBECONFIG` | Path to kubeconfig (local dev only) | in-cluster config |

## Security Considerations

This dashboard is designed as an **internal tool** and does **not** include built-in authentication or authorization. As described above, the tool is accessible when port-forwarding from the cluster where it's running. Future development may include auth.

## Local Development

```bash
pip install -r requirements.txt

# Set DATABASE_URL and KUBECONFIG, then:
uvicorn app.main:app --reload --port 8000
```

## Project Structure

```
fournos-ui/
├── app/
│   ├── main.py              # FastAPI routes and Jinja2 rendering
│   ├── config.py            # Environment-based settings
│   ├── db.py                # SQLAlchemy models and queries
│   ├── k8s_client.py        # Kubernetes API wrapper (with timeouts)
│   ├── watcher.py           # Background FournosJob event watcher
│   ├── forge_discovery.py   # Project discovery from ConfigMap
│   ├── models.py            # Pydantic/dataclass models
│   ├── static/              # CSS, HTMX, htmx-sse.js
│   └── templates/           # Jinja2 HTML templates
├── kustomize/
│   ├── base/                # Generic K8s manifests
│   └── overlays/ocp/        # Environment-specific overrides
├── Dockerfile
└── requirements.txt
```
