"""Kubernetes client wrapper for FournosJob, PipelineRun, Pod, and CronJob operations."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Generator

import yaml
from kubernetes import client, config, watch
from kubernetes.client.rest import ApiException

from app.config import settings

logger = logging.getLogger(__name__)

_api_client: client.ApiClient | None = None
_custom_api: client.CustomObjectsApi | None = None
_core_api: client.CoreV1Api | None = None
_batch_api: client.BatchV1Api | None = None
_lock = threading.Lock()


def _ensure_loaded() -> None:
    """Load kubeconfig or in-cluster config once."""
    global _api_client, _custom_api, _core_api, _batch_api
    if _custom_api is not None:
        return
    with _lock:
        if _custom_api is not None:
            return
        try:
            if settings.kubeconfig_path:
                config.load_kube_config(config_file=settings.kubeconfig_path)
            else:
                try:
                    config.load_incluster_config()
                except config.ConfigException:
                    config.load_kube_config()
        except Exception:
            logger.warning("K8s config not available -- running in offline mode")
            return

        _api_client = client.ApiClient()
        _custom_api = client.CustomObjectsApi(_api_client)
        _core_api = client.CoreV1Api(_api_client)
        _batch_api = client.BatchV1Api(_api_client)
        logger.info("Kubernetes client initialised")


def is_connected() -> bool:
    """Return True if a K8s client has been successfully loaded."""
    _ensure_loaded()
    return _custom_api is not None


# ---------------------------------------------------------------------------
# FournosJob operations
# ---------------------------------------------------------------------------

def list_fournos_jobs(namespace: str | None = None) -> list[dict]:
    """List all FournosJob CRs in the given namespace."""
    _ensure_loaded()
    if _custom_api is None:
        return []
    ns = namespace or settings.fournos_namespace
    try:
        result = _custom_api.list_namespaced_custom_object(
            group=settings.fournos_api_group,
            version=settings.fournos_api_version,
            namespace=ns,
            plural=settings.fournos_job_plural,
        )
        return result.get("items", [])
    except ApiException as exc:
        logger.error("Failed to list FournosJobs: %s", exc.reason)
        return []


def get_fournos_job(name: str, namespace: str | None = None) -> dict | None:
    """Get a specific FournosJob by name."""
    _ensure_loaded()
    if _custom_api is None:
        return None
    ns = namespace or settings.fournos_namespace
    try:
        return _custom_api.get_namespaced_custom_object(
            group=settings.fournos_api_group,
            version=settings.fournos_api_version,
            namespace=ns,
            plural=settings.fournos_job_plural,
            name=name,
        )
    except ApiException as exc:
        if exc.status == 404:
            return None
        logger.error("Failed to get FournosJob %s: %s", name, exc.reason)
        return None


def create_fournos_job(body: dict, namespace: str | None = None) -> dict:
    """Create a new FournosJob CR."""
    _ensure_loaded()
    if _custom_api is None:
        raise RuntimeError("Kubernetes client not available")
    ns = namespace or settings.fournos_namespace
    return _custom_api.create_namespaced_custom_object(
        group=settings.fournos_api_group,
        version=settings.fournos_api_version,
        namespace=ns,
        plural=settings.fournos_job_plural,
        body=body,
    )


def patch_fournos_job(
    name: str, patch: dict, namespace: str | None = None
) -> dict:
    """Patch a FournosJob (e.g. set spec.shutdown)."""
    _ensure_loaded()
    if _custom_api is None:
        raise RuntimeError("Kubernetes client not available")
    ns = namespace or settings.fournos_namespace
    return _custom_api.patch_namespaced_custom_object(
        group=settings.fournos_api_group,
        version=settings.fournos_api_version,
        namespace=ns,
        plural=settings.fournos_job_plural,
        name=name,
        body=patch,
    )


def shutdown_fournos_job(
    name: str, value: str = "Stop", namespace: str | None = None
) -> dict:
    """Set spec.shutdown on a FournosJob to cancel it."""
    return patch_fournos_job(name, {"spec": {"shutdown": value}}, namespace)


def watch_fournos_jobs(
    namespace: str | None = None,
    resource_version: str = "",
    timeout: int = 0,
) -> Generator[dict, None, None]:
    """Yield watch events for FournosJobs. Blocks until timeout or stream ends."""
    _ensure_loaded()
    if _custom_api is None:
        return
    ns = namespace or settings.fournos_namespace
    w = watch.Watch()
    kwargs: dict[str, Any] = {
        "group": settings.fournos_api_group,
        "version": settings.fournos_api_version,
        "namespace": ns,
        "plural": settings.fournos_job_plural,
    }
    if resource_version:
        kwargs["resource_version"] = resource_version
    if timeout:
        kwargs["timeout_seconds"] = timeout
    try:
        for event in w.stream(_custom_api.list_namespaced_custom_object, **kwargs):
            yield event
    except ApiException as exc:
        logger.warning("Watch stream ended: %s", exc.reason)


# ---------------------------------------------------------------------------
# Tekton PipelineRun operations
# ---------------------------------------------------------------------------

def get_pipelinerun(name: str, namespace: str | None = None) -> dict | None:
    """Get a Tekton PipelineRun by name."""
    _ensure_loaded()
    if _custom_api is None:
        return None
    ns = namespace or settings.fournos_namespace
    try:
        return _custom_api.get_namespaced_custom_object(
            group=settings.tekton_api_group,
            version=settings.tekton_api_version,
            namespace=ns,
            plural=settings.tekton_pipelinerun_plural,
            name=name,
        )
    except ApiException as exc:
        if exc.status == 404:
            return None
        logger.error("Failed to get PipelineRun %s: %s", name, exc.reason)
        return None


def list_pipelineruns_for_job(
    job_name: str, namespace: str | None = None
) -> list[dict]:
    """List PipelineRuns associated with a FournosJob (by label)."""
    _ensure_loaded()
    if _custom_api is None:
        return []
    ns = namespace or settings.fournos_namespace
    try:
        result = _custom_api.list_namespaced_custom_object(
            group=settings.tekton_api_group,
            version=settings.tekton_api_version,
            namespace=ns,
            plural=settings.tekton_pipelinerun_plural,
            label_selector=f"fournos.dev/job-name={job_name}",
        )
        return result.get("items", [])
    except ApiException as exc:
        logger.error("Failed to list PipelineRuns for %s: %s", job_name, exc.reason)
        return []


def get_taskrun(name: str, namespace: str | None = None) -> dict | None:
    """Get a Tekton TaskRun by name."""
    _ensure_loaded()
    if _custom_api is None:
        return None
    ns = namespace or settings.fournos_namespace
    try:
        return _custom_api.get_namespaced_custom_object(
            group=settings.tekton_api_group,
            version=settings.tekton_api_version,
            namespace=ns,
            plural="taskruns",
            name=name,
        )
    except ApiException as exc:
        if exc.status == 404:
            return None
        logger.error("Failed to get TaskRun %s: %s", name, exc.reason)
        return None


def _phase_from_conditions(conditions: list[dict]) -> str:
    """Determine task phase from Tekton conditions."""
    if not conditions:
        return "Pending"
    cond = conditions[0]
    reason = cond.get("reason", "")
    cond_status = cond.get("status", "")
    if reason == "Succeeded" and cond_status == "True":
        return "Succeeded"
    if reason == "Failed" or cond_status == "False":
        return "Failed"
    if reason in ("Running", "Started"):
        return "Running"
    if reason == "TaskRunCancelled":
        return "Cancelled"
    if reason == "SkippingNoMatch":
        return "Skipped"
    return "Pending"


def get_current_step_for_job(
    job_name: str, namespace: str | None = None
) -> dict | None:
    """Return the currently running pipeline step for a job, or None.

    Result dict has keys: name, displayName, startTime.
    """
    prs = list_pipelineruns_for_job(job_name, namespace)
    if not prs:
        return None

    child_refs = prs[0].get("status", {}).get("childReferences", [])
    for ref in child_refs:
        task_run_name = ref.get("name", "")
        tr = get_taskrun(task_run_name)
        if not tr:
            continue
        tr_status = tr.get("status", {})
        phase = _phase_from_conditions(tr_status.get("conditions", []))
        if phase == "Running":
            task_name = ref.get("pipelineTaskName", task_run_name)
            return {
                "name": task_name,
                "displayName": task_name.replace("-", " ").title(),
                "startTime": tr_status.get("startTime"),
            }
    return None


def extract_pipeline_stages(pipelinerun: dict) -> list[dict]:
    """Extract stage information from a PipelineRun status for timeline display."""
    status = pipelinerun.get("status", {})
    child_refs = status.get("childReferences", [])
    pipeline_spec = status.get("pipelineSpec", {})

    finally_task_names = set()
    for task in pipeline_spec.get("finally", []):
        finally_task_names.add(task.get("name", ""))

    stages = []
    for ref in child_refs:
        task_name = ref.get("pipelineTaskName", ref.get("name", "unknown"))
        task_run_name = ref.get("name", "")

        start_time = None
        completion_time = None
        task_phase = "Pending"

        tr = get_taskrun(task_run_name)
        if tr:
            tr_status = tr.get("status", {})
            start_time = tr_status.get("startTime")
            completion_time = tr_status.get("completionTime")
            task_phase = _phase_from_conditions(tr_status.get("conditions", []))

        display_name = task_name.replace("-", " ").title()

        stages.append({
            "name": task_name,
            "displayName": display_name,
            "status": task_phase,
            "startTime": start_time,
            "completionTime": completion_time,
            "finally": task_name in finally_task_names,
        })

    stages.sort(key=lambda s: (s["finally"], s.get("startTime") or "9999"))
    return stages


# ---------------------------------------------------------------------------
# Pod operations
# ---------------------------------------------------------------------------

def list_pods_for_job(job_name: str, namespace: str | None = None) -> list[dict]:
    """List pods associated with a FournosJob."""
    _ensure_loaded()
    if _core_api is None:
        return []
    ns = namespace or settings.fournos_namespace
    try:
        result = _core_api.list_namespaced_pod(
            namespace=ns,
            label_selector=f"fournos.dev/job-name={job_name}",
        )
        pods = []
        for pod in result.items:
            created = pod.metadata.creation_timestamp
            age_minutes = 0
            if created:
                delta = datetime.now(timezone.utc) - created.replace(tzinfo=timezone.utc)
                age_minutes = int(delta.total_seconds() / 60)

            container_ready = False
            restarts = 0
            if pod.status.container_statuses:
                for cs in pod.status.container_statuses:
                    if cs.ready:
                        container_ready = True
                    restarts += cs.restart_count

            if pod.metadata.name.startswith("affinity-assistant"):
                continue

            pods.append({
                "name": pod.metadata.name,
                "phase": pod.status.phase or "Unknown",
                "container": (
                    pod.spec.containers[0].name if pod.spec.containers else "unknown"
                ),
                "ready": container_ready,
                "restarts": restarts,
                "age_minutes": age_minutes,
            })
        return pods
    except ApiException as exc:
        logger.error("Failed to list pods for %s: %s", job_name, exc.reason)
        return []


def read_pod_log(
    pod_name: str,
    namespace: str | None = None,
    container: str | None = None,
    follow: bool = False,
    tail_lines: int | None = None,
) -> Generator[str, None, None]:
    """Stream or read pod logs line by line."""
    _ensure_loaded()
    if _core_api is None:
        yield "Kubernetes client not available"
        return
    ns = namespace or settings.fournos_namespace
    kwargs: dict[str, Any] = {"name": pod_name, "namespace": ns, "follow": follow}
    if container:
        kwargs["container"] = container
    if tail_lines:
        kwargs["tail_lines"] = tail_lines
    try:
        if follow:
            for line in _core_api.read_namespaced_pod_log(**kwargs, _preload_content=False).stream():
                decoded = line.decode("utf-8", errors="replace").rstrip("\n")
                yield decoded
        else:
            log_text = _core_api.read_namespaced_pod_log(**kwargs)
            for line in log_text.splitlines():
                yield line
    except ApiException as exc:
        yield f"Error reading logs: {exc.reason}"


def read_pod_log_full(
    pod_name: str,
    namespace: str | None = None,
    container: str | None = None,
) -> str:
    """Read entire pod log as a single string (for archival)."""
    _ensure_loaded()
    if _core_api is None:
        return ""
    ns = namespace or settings.fournos_namespace
    kwargs: dict[str, Any] = {"name": pod_name, "namespace": ns}
    if container:
        kwargs["container"] = container
    try:
        return _core_api.read_namespaced_pod_log(**kwargs)
    except ApiException as exc:
        logger.error("Failed to read full log for %s: %s", pod_name, exc.reason)
        return f"Error: {exc.reason}"


# ---------------------------------------------------------------------------
# CronJob operations (for scheduling)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------

SCHEDULE_LABEL = "fournos-launcher/managed-by"
SCHEDULE_LABEL_VALUE = "fournos-dashboard"
PROJECT_LABEL = "fournos-launcher/project"


def list_managed_cronjobs(namespace: str | None = None) -> list[dict]:
    """List CronJobs managed by the dashboard."""
    _ensure_loaded()
    if _batch_api is None:
        return []
    ns = namespace or settings.fournos_namespace
    try:
        result = _batch_api.list_namespaced_cron_job(
            namespace=ns,
            label_selector=f"{SCHEDULE_LABEL}={SCHEDULE_LABEL_VALUE}",
        )
        return [_cronjob_to_dict(cj) for cj in result.items]
    except ApiException as exc:
        logger.error("Failed to list CronJobs: %s", exc.reason)
        return []


def get_managed_cronjob(name: str, namespace: str | None = None) -> dict | None:
    """Get a specific managed CronJob."""
    _ensure_loaded()
    if _batch_api is None:
        return None
    ns = namespace or settings.fournos_namespace
    try:
        cj = _batch_api.read_namespaced_cron_job(name=name, namespace=ns)
        return _cronjob_to_dict(cj)
    except ApiException as exc:
        if exc.status == 404:
            return None
        logger.error("Failed to get CronJob %s: %s", name, exc.reason)
        return None


def create_cronjob(
    name: str,
    schedule: str,
    project: str,
    cluster: str,
    pipeline: str,
    preset: str,
    image: str,
    owner: str = "",
    config_overrides: dict | None = None,
    resolver_script: str = "",
    resolver_image: str = "",
    resolver_filename: str = "",
    namespace: str | None = None,
) -> dict:
    """Create a K8s CronJob that submits a FournosJob on schedule.

    If *resolver_script* is provided, the CronJob pod gets an init container
    that runs the script and writes KEY=VALUE pairs to /shared/resolved.env.
    The main submit container reads those values and injects them as
    configOverrides into the FournosJob spec before submission.

    The file extension of *resolver_filename* determines the interpreter:
    .py -> python, .sh (or default) -> sh -c.
    """
    _ensure_loaded()
    if _batch_api is None:
        raise RuntimeError("Kubernetes client not available")
    ns = namespace or settings.fournos_namespace

    fjob_spec = {
        "apiVersion": f"{settings.fournos_api_group}/{settings.fournos_api_version}",
        "kind": "FournosJob",
        "metadata": {
            "generateName": f"forge-{project.replace('_', '-')}-sched-",
            "namespace": ns,
            "labels": {
                "fournos-launcher/schedule-name": name,
                "fournos-launcher/trigger-type": "scheduled",
            },
        },
        "spec": {
            "cluster": cluster,
            "displayName": f"{project} {preset}".strip(),
            "owner": owner or "fournos-dashboard/scheduler",
            "pipeline": pipeline,
            "exclusive": True,
            "executionEngine": {
                "forge": {
                    "project": project,
                    "args": [preset] if preset else [],
                    "configOverrides": config_overrides or {},
                }
            },
        },
    }
    fjob_json = json.dumps(fjob_spec)
    api_path = (
        f"/apis/{settings.fournos_api_group}/{settings.fournos_api_version}"
        f"/namespaces/{ns}/{settings.fournos_job_plural}"
    )

    submit_common = (
        "token = open('/var/run/secrets/kubernetes.io/serviceaccount/token').read()\n"
        "ctx = ssl.create_default_context(cafile='/var/run/secrets/kubernetes.io/serviceaccount/ca.crt')\n"
        f"url = 'https://kubernetes.default.svc{api_path}'\n"
        "print('Submitting FournosJob to', url)\n"
        "print('Body:', json.dumps(body, indent=2))\n"
        "data = json.dumps(body).encode()\n"
        "req = urllib.request.Request(url, data=data, method='POST',\n"
        "    headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})\n"
        "try:\n"
        "    resp = urllib.request.urlopen(req, context=ctx)\n"
        "    result = json.loads(resp.read())\n"
        "    print('FournosJob submitted:', result['metadata'].get('name', 'unknown'))\n"
        "except urllib.error.HTTPError as e:\n"
        "    print('API error:', e.code, e.reason)\n"
        "    print(e.read().decode())\n"
        "    raise\n"
    )

    trigger_override = (
        "trigger = os.environ.get('FOURNOS_TRIGGER_TYPE', 'scheduled')\n"
        "body['metadata'].setdefault('labels', {})['fournos-launcher/trigger-type'] = trigger\n"
    )

    if resolver_script:
        submit_script = (
            "import json, os, ssl, urllib.request, urllib.error\n"
            "body = json.loads(os.environ['FJOB_JSON'])\n"
            + trigger_override
            + "overrides = body['spec']['executionEngine']['forge'].setdefault('configOverrides', {})\n"
            "env_file = '/shared/resolved.env'\n"
            "if os.path.exists(env_file):\n"
            "    with open(env_file) as f:\n"
            "        for line in f:\n"
            "            line = line.strip()\n"
            "            if '=' in line and not line.startswith('#'):\n"
            "                k, v = line.split('=', 1)\n"
            "                overrides[k.strip()] = v.strip()\n"
            "                print(f'Resolved: {k.strip()} = {v.strip()}')\n"
            + submit_common
        )
    else:
        submit_script = (
            "import json, os, ssl, urllib.request, urllib.error\n"
            "body = json.loads(os.environ['FJOB_JSON'])\n"
            + trigger_override
            + submit_common
        )

    submit_container = client.V1Container(
        name="submit",
        image=image or "python:3.12-slim",
        command=["python", "-c", submit_script],
        env=[client.V1EnvVar(name="FJOB_JSON", value=fjob_json)],
    )

    init_containers = None
    volumes = None

    if resolver_script:
        is_python = resolver_filename.lower().endswith(".py")
        default_resolver_img = "python:3.12-slim" if is_python else "alpine:latest"
        script_key = resolver_filename or ("resolver.py" if is_python else "resolver.sh")
        configmap_name = f"{name}-resolver"

        _create_resolver_configmap(configmap_name, script_key, resolver_script, ns)

        script_vol = client.V1Volume(
            name="resolver-script",
            config_map=client.V1ConfigMapVolumeSource(
                name=configmap_name,
                default_mode=0o755,
            ),
        )
        shared_vol = client.V1Volume(
            name="shared",
            empty_dir=client.V1EmptyDirVolumeSource(),
        )
        volumes = [script_vol, shared_vol]
        script_mount = client.V1VolumeMount(
            name="resolver-script", mount_path="/resolver", read_only=True,
        )
        shared_mount = client.V1VolumeMount(name="shared", mount_path="/shared")
        submit_container.volume_mounts = [shared_mount]

        if is_python:
            resolver_cmd = ["python", f"/resolver/{script_key}"]
        else:
            resolver_cmd = ["sh", f"/resolver/{script_key}"]

        init_containers = [
            client.V1Container(
                name="resolver",
                image=resolver_image or default_resolver_img,
                command=resolver_cmd,
                volume_mounts=[script_mount, shared_mount],
            )
        ]

    annotations = {
        "fournos-launcher/project": project,
        "fournos-launcher/cluster": cluster,
        "fournos-launcher/pipeline": pipeline,
        "fournos-launcher/preset": preset,
        "fournos-launcher/owner": owner,
    }
    if resolver_script:
        annotations["fournos-launcher/resolver-configmap"] = configmap_name
        annotations["fournos-launcher/resolver-filename"] = script_key
    if resolver_image:
        annotations["fournos-launcher/resolver-image"] = resolver_image

    cj_body = client.V1CronJob(
        api_version="batch/v1",
        kind="CronJob",
        metadata=client.V1ObjectMeta(
            name=name,
            namespace=ns,
            labels={
                SCHEDULE_LABEL: SCHEDULE_LABEL_VALUE,
                PROJECT_LABEL: project,
            },
            annotations=annotations,
        ),
        spec=client.V1CronJobSpec(
            schedule=schedule,
            suspend=False,
            successful_jobs_history_limit=3,
            failed_jobs_history_limit=3,
            job_template=client.V1JobTemplateSpec(
                spec=client.V1JobSpec(
                    template=client.V1PodTemplateSpec(
                        spec=client.V1PodSpec(
                            init_containers=init_containers,
                            containers=[submit_container],
                            volumes=volumes,
                            service_account_name="fournos-dashboard-sa",
                            restart_policy="Never",
                        )
                    ),
                    backoff_limit=0,
                )
            ),
        ),
    )

    result = _batch_api.create_namespaced_cron_job(namespace=ns, body=cj_body)
    return _cronjob_to_dict(result)


def _create_resolver_configmap(
    cm_name: str, script_key: str, script_content: str, namespace: str
) -> None:
    """Create a ConfigMap to hold a resolver script file."""
    _ensure_loaded()
    if _core_api is None:
        raise RuntimeError("Kubernetes client not available")
    cm = client.V1ConfigMap(
        api_version="v1",
        kind="ConfigMap",
        metadata=client.V1ObjectMeta(
            name=cm_name,
            namespace=namespace,
            labels={
                SCHEDULE_LABEL: SCHEDULE_LABEL_VALUE,
                "fournos-launcher/type": "resolver-script",
            },
        ),
        data={script_key: script_content},
    )
    try:
        _core_api.create_namespaced_config_map(namespace=namespace, body=cm)
    except ApiException as exc:
        if exc.status == 409:
            _core_api.replace_namespaced_config_map(
                name=cm_name, namespace=namespace, body=cm,
            )
        else:
            raise


def get_resolver_script(configmap_name: str, namespace: str | None = None) -> tuple[str, str]:
    """Read the resolver script from its ConfigMap. Returns (filename, content)."""
    _ensure_loaded()
    if _core_api is None:
        return "", ""
    ns = namespace or settings.fournos_namespace
    try:
        cm = _core_api.read_namespaced_config_map(name=configmap_name, namespace=ns)
        if cm.data:
            for key, value in cm.data.items():
                return key, value
    except ApiException:
        pass
    return "", ""


def _delete_resolver_configmap(cm_name: str, namespace: str) -> None:
    """Delete a resolver ConfigMap (best-effort)."""
    _ensure_loaded()
    if _core_api is None:
        return
    try:
        _core_api.delete_namespaced_config_map(name=cm_name, namespace=namespace)
    except ApiException:
        pass


def trigger_cronjob(name: str, namespace: str | None = None) -> str:
    """Manually trigger a CronJob by creating a one-off Job from its spec."""
    _ensure_loaded()
    if _batch_api is None:
        raise RuntimeError("Kubernetes client not available")
    ns = namespace or settings.fournos_namespace

    cj = _batch_api.read_namespaced_cron_job(name=name, namespace=ns)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    job_name = f"{name}-manual-{ts}"[:63]

    job_spec = cj.spec.job_template.spec

    trigger_env = client.V1EnvVar(name="FOURNOS_TRIGGER_TYPE", value="manual")
    if job_spec.template and job_spec.template.spec and job_spec.template.spec.containers:
        for c in job_spec.template.spec.containers:
            if c.env is None:
                c.env = []
            c.env.append(trigger_env)

    job_body = client.V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=client.V1ObjectMeta(
            name=job_name,
            namespace=ns,
            labels={
                SCHEDULE_LABEL: SCHEDULE_LABEL_VALUE,
                "fournos-launcher/triggered-by": "manual",
            },
            annotations={"cronjob.kubernetes.io/instantiate": "manual"},
        ),
        spec=job_spec,
    )

    _batch_api.create_namespaced_job(namespace=ns, body=job_body)
    return job_name


def delete_cronjob(name: str, namespace: str | None = None) -> None:
    """Delete a managed CronJob and its resolver ConfigMap if present."""
    _ensure_loaded()
    if _batch_api is None:
        raise RuntimeError("Kubernetes client not available")
    ns = namespace or settings.fournos_namespace
    cj = get_managed_cronjob(name, ns)
    _batch_api.delete_namespaced_cron_job(
        name=name,
        namespace=ns,
        propagation_policy="Foreground",
    )
    if cj and cj.get("resolver_configmap"):
        _delete_resolver_configmap(cj["resolver_configmap"], ns)


def patch_cronjob_suspend(
    name: str, suspend: bool, namespace: str | None = None
) -> dict:
    """Pause or resume a CronJob by toggling spec.suspend."""
    _ensure_loaded()
    if _batch_api is None:
        raise RuntimeError("Kubernetes client not available")
    ns = namespace or settings.fournos_namespace
    result = _batch_api.patch_namespaced_cron_job(
        name=name,
        namespace=ns,
        body={"spec": {"suspend": suspend}},
    )
    return _cronjob_to_dict(result)


def _cronjob_to_dict(cj: Any) -> dict:
    """Serialise a V1CronJob into a plain dict for templates."""
    meta = cj.metadata
    annotations = meta.annotations or {}
    resolver_configmap = annotations.get("fournos-launcher/resolver-configmap", "")
    resolver_image = annotations.get("fournos-launcher/resolver-image", "")
    resolver_filename = annotations.get("fournos-launcher/resolver-filename", "")
    return {
        "name": meta.name,
        "namespace": meta.namespace,
        "schedule": cj.spec.schedule,
        "suspend": cj.spec.suspend or False,
        "project": annotations.get("fournos-launcher/project", ""),
        "cluster": annotations.get("fournos-launcher/cluster", ""),
        "pipeline": annotations.get("fournos-launcher/pipeline", ""),
        "preset": annotations.get("fournos-launcher/preset", ""),
        "owner": annotations.get("fournos-launcher/owner", ""),
        "resolver_configmap": resolver_configmap,
        "resolver_image": resolver_image,
        "resolver_filename": resolver_filename,
        "has_resolver": bool(resolver_configmap),
        "created_at": meta.creation_timestamp.isoformat() if meta.creation_timestamp else "",
        "last_schedule": (
            cj.status.last_schedule_time.isoformat()
            if cj.status and cj.status.last_schedule_time
            else ""
        ),
        "active_count": len(cj.status.active) if cj.status and cj.status.active else 0,
    }
