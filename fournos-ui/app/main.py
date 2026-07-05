"""Fournos Launcher Dashboard -- production FastAPI application."""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader

from app import db, k8s_client, watcher
from app.config import settings
from app.forge_discovery import discover_projects, get_project_presets

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=getattr(logging, settings.log_level))
    await db.init_db()
    watcher.start_watcher()
    yield

app = FastAPI(title="Fournos Launcher Dashboard", lifespan=lifespan)

BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

_jinja_env = Environment(
    loader=FileSystemLoader(str(BASE_DIR / "templates")),
    autoescape=True,
    cache_size=0,
)

# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------

def _format_age(timestamp_str: str) -> str:
    from dateutil.parser import parse

    try:
        created = parse(timestamp_str)
    except Exception:
        return "?"
    delta = datetime.now(timezone.utc) - created
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        return "0s"
    if total_seconds < 60:
        return f"{total_seconds}s"
    if total_seconds < 3600:
        return f"{total_seconds // 60}m"
    hours = total_seconds // 3600
    mins = (total_seconds % 3600) // 60
    if hours < 24:
        return f"{hours}h {mins}m"
    days = hours // 24
    return f"{days}d {hours % 24}h"


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    s = int(seconds)
    h, remainder = divmod(s, 3600)
    m, sec = divmod(remainder, 60)
    return f"{h:02d}h {m:02d}m {sec:02d}s"


def _phase_class(phase: str) -> str:
    return {
        "Running": "phase-running",
        "Succeeded": "phase-succeeded",
        "Failed": "phase-failed",
        "Stopped": "phase-stopped",
        "Resolving": "phase-resolving",
        "Pending": "phase-resolving",
    }.get(phase, "phase-unknown")


def _extract_forge_info(job: dict) -> dict:
    forge = job.get("spec", {}).get("executionEngine", {}).get("forge", {})
    env = job.get("spec", {}).get("env", {})
    pr_number = env.get("PULL_NUMBER", "")
    pr_title = env.get("PULL_TITLE", "")
    repo_owner = env.get("REPO_OWNER", "")
    repo_name = env.get("REPO_NAME", "")
    pr_url = f"https://github.com/{repo_owner}/{repo_name}/pull/{pr_number}" if pr_number else ""
    return {
        "project": forge.get("project", ""),
        "args": forge.get("args", []),
        "config_overrides": forge.get("configOverrides", {}),
        "pr_number": pr_number,
        "pr_title": pr_title,
        "pr_url": pr_url,
    }


def _parse_task_progress(message: str) -> dict | None:
    m = re.search(
        r"Tasks Completed:\s*(\d+)\s*\(Failed:\s*(\d+),\s*Cancelled\s*(\d+)\),\s*Incomplete:\s*(\d+),\s*Skipped:\s*(\d+)",
        message,
    )
    if not m:
        return None
    return {
        "completed": int(m.group(1)),
        "failed": int(m.group(2)),
        "cancelled": int(m.group(3)),
        "incomplete": int(m.group(4)),
        "skipped": int(m.group(5)),
        "total": int(m.group(1)) + int(m.group(4)) + int(m.group(5)),
    }


def _build_timeline(stages: list[dict]) -> list[dict]:
    from dateutil.parser import parse

    now = datetime.now(timezone.utc)
    n = len(stages) or 1
    equal_pct = 100.0 / n

    result = []
    for s in stages:
        start = parse(s["startTime"]) if s["startTime"] else None
        end = parse(s["completionTime"]) if s["completionTime"] else None
        if start and end:
            dur = (end - start).total_seconds()
        elif start:
            dur = (now - start).total_seconds()
        else:
            dur = 0
        dur = max(dur, 0)

        if dur < 60:
            dur_label = f"{int(dur)}s"
        elif dur < 3600:
            dur_label = f"{int(dur // 60)}m {int(dur % 60)}s"
        else:
            dur_label = f"{int(dur // 3600)}h {int((dur % 3600) // 60)}m"

        status_class = {
            "Succeeded": "ptl-ok",
            "Running": "ptl-run",
            "Failed": "ptl-err",
            "Pending": "ptl-wait",
            "Cancelled": "ptl-cancel",
            "Skipped": "ptl-skip",
        }.get(s["status"], "ptl-wait")

        result.append({
            **s,
            "width_pct": equal_pct,
            "min_width": 8,
            "duration_label": dur_label if s["startTime"] else "",
            "status_class": status_class,
        })
    return result


def _extract_mlflow_url(status: dict) -> str:
    """Extract MLflow run URL from FournosJob status."""
    mlflow = (
        status.get("engineStatus", {})
        .get("forge", {})
        .get("exportArtifacts", {})
        .get("caliper_artifacts_export", {})
        .get("backends", {})
        .get("mlflow", {})
    )
    return mlflow.get("run_url", "") if mlflow else ""


_CACHE_BUST = str(int(datetime.now(timezone.utc).timestamp()))

_jinja_env.globals.update(
    format_age=_format_age,
    format_duration=_format_duration,
    phase_class=_phase_class,
    extract_forge_info=_extract_forge_info,
    parse_task_progress=_parse_task_progress,
    build_timeline=_build_timeline,
    extract_mlflow_url=_extract_mlflow_url,
    url_for=lambda name, **kw: app.url_path_for(name, **kw),
    cache_bust=_CACHE_BUST,
)


_NAV_MAP = {
    "jobs_list.html": "jobs",
    "job_detail.html": "jobs",
    "components/jobs_table_body.html": "jobs",
    "submit_job.html": "submit",
    "schedules.html": "schedules",
    "schedule_runs.html": "schedules",
}


def _render(template_name: str, **context: Any) -> HTMLResponse:
    context.setdefault("active_nav", _NAV_MAP.get(template_name, ""))
    tpl = _jinja_env.get_template(template_name)
    return HTMLResponse(tpl.render(**context))


# ---------------------------------------------------------------------------
# Data fetching helpers
# ---------------------------------------------------------------------------

_COMPLETED_GRACE_SECONDS = 180  # keep completed jobs on Live tab for 3 minutes


def _get_live_jobs() -> list[dict]:
    """Get FournosJobs from K8s, sorted newest-first, hiding old completed jobs."""
    from dateutil.parser import parse

    jobs = k8s_client.list_fournos_jobs()
    now = datetime.now(timezone.utc)
    visible: list[dict] = []
    for j in jobs:
        phase = j.get("status", {}).get("phase", "")
        if phase in ("Succeeded", "Failed", "Stopped"):
            conditions = j.get("status", {}).get("conditions", [])
            last_ts = None
            for c in conditions:
                ts_str = c.get("lastTransitionTime")
                if ts_str:
                    try:
                        last_ts = parse(ts_str)
                    except Exception:
                        pass
            if last_ts and (now - last_ts).total_seconds() > _COMPLETED_GRACE_SECONDS:
                continue
        visible.append(j)

    visible.sort(
        key=lambda j: j.get("metadata", {}).get("creationTimestamp", ""),
        reverse=True,
    )
    return visible


def _compute_current_steps(jobs: list[dict]) -> dict[str, dict]:
    """For each running job, fetch the currently active pipeline step."""
    steps: dict[str, dict] = {}
    for j in jobs:
        phase = j.get("status", {}).get("phase", "")
        if phase not in ("Running", "Admitted"):
            continue
        name = j.get("metadata", {}).get("name", "")
        try:
            step = k8s_client.get_current_step_for_job(name)
            if step:
                steps[name] = step
        except Exception:
            pass
    return steps


def _get_pipeline_stages(job: dict) -> list[dict]:
    """Get pipeline stages for a job from its PipelineRun."""
    job_name = job.get("metadata", {}).get("name", "")

    pr_name = job.get("status", {}).get("pipelineRun", "")
    if pr_name:
        pr = k8s_client.get_pipelinerun(pr_name)
        if pr:
            return k8s_client.extract_pipeline_stages(pr)

    prs = k8s_client.list_pipelineruns_for_job(job_name)
    if prs:
        return k8s_client.extract_pipeline_stages(prs[0])

    return []


# ---------------------------------------------------------------------------
# Routes: Jobs
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def jobs_list(
    request: Request,
    tab: str = Query("live", pattern="^(live|history)$"),
    project: str = Query("", alias="project"),
    cluster: str = Query("", alias="cluster"),
    status: str = Query("", alias="status"),
    owner: str = Query("", alias="owner"),
    page: int = Query(1, ge=1),
):
    per_page = 50
    filters = {"project": project, "cluster": cluster, "status": status, "owner": owner}

    if tab == "live":
        jobs = _get_live_jobs()
        if project:
            jobs = [j for j in jobs if _extract_forge_info(j).get("project") == project]
        if cluster:
            jobs = [j for j in jobs if j.get("spec", {}).get("cluster") == cluster]
        if status:
            jobs = [j for j in jobs if j.get("status", {}).get("phase") == status]
        if owner:
            jobs = [j for j in jobs if j.get("spec", {}).get("owner") == owner]
        total = len(jobs)
        history_jobs = []
        total_history = 0
    else:
        jobs = []
        async with db.async_session() as session:
            history_jobs_db, total_history = await db.list_jobs(
                session,
                project=project or None,
                cluster=cluster or None,
                status=status or None,
                owner=owner or None,
                limit=per_page,
                offset=(page - 1) * per_page,
            )
            history_jobs = [_db_job_to_dict(j) for j in history_jobs_db]
        total = total_history

    projects_list = [p.name for p in discover_projects()]
    clusters = _collect_clusters(jobs)
    current_steps = _compute_current_steps(jobs) if tab == "live" else {}

    return _render(
        "jobs_list.html",
        jobs=jobs,
        history_jobs=history_jobs,
        tab=tab,
        filters=filters,
        projects=projects_list,
        clusters=clusters,
        page=page,
        per_page=per_page,
        total=total,
        current_steps=current_steps,
    )


@app.get("/api/jobs-table", response_class=HTMLResponse)
async def jobs_table_partial(
    request: Request,
    project: str = Query(""),
    cluster: str = Query(""),
    status: str = Query(""),
    owner: str = Query(""),
):
    jobs = _get_live_jobs()
    if project:
        jobs = [j for j in jobs if _extract_forge_info(j).get("project") == project]
    if cluster:
        jobs = [j for j in jobs if j.get("spec", {}).get("cluster") == cluster]
    if status:
        jobs = [j for j in jobs if j.get("status", {}).get("phase") == status]
    if owner:
        jobs = [j for j in jobs if j.get("spec", {}).get("owner") == owner]
    current_steps = _compute_current_steps(jobs)
    return _render("components/jobs_table_body.html", jobs=jobs, current_steps=current_steps)


@app.get("/jobs/{job_name}", response_class=HTMLResponse)
async def job_detail(request: Request, job_name: str):
    job = None
    source = "live"
    pods: list[dict] = []
    stages: list[dict] = []

    job = k8s_client.get_fournos_job(job_name)
    if job:
        pods = k8s_client.list_pods_for_job(job_name)
        stages = _get_pipeline_stages(job)

    if not job:
        source = "history"
        async with db.async_session() as session:
            db_job = await db.get_job_by_name(session, job_name)
            if db_job is None:
                raise HTTPException(status_code=404, detail="Job not found")

            job = _db_job_to_fjob_dict(db_job)

    return _render(
        "job_detail.html",
        job=job,
        pods=pods,
        stages=stages,
        source=source,
    )


@app.get("/api/jobs/{job_name}/detail-partial", response_class=HTMLResponse)
async def job_detail_partial(request: Request, job_name: str):
    """Return the dynamic portions of the job detail page for HTMX polling."""
    job = k8s_client.get_fournos_job(job_name)
    if not job:
        return HTMLResponse("")
    pods = k8s_client.list_pods_for_job(job_name)
    stages = _get_pipeline_stages(job)
    return _render(
        "components/job_detail_dynamic.html",
        job=job,
        pods=pods,
        stages=stages,
    )


@app.post("/api/jobs/{job_name}/cancel")
async def cancel_job(job_name: str):
    try:
        k8s_client.shutdown_fournos_job(job_name)
        return {"status": "ok", "message": f"Shutdown requested for {job_name}"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/jobs/{job_name}/rerun")
async def rerun_job(job_name: str):
    """Clone an existing FournosJob's spec into a brand-new job."""
    job = await _get_job_for_rerun(job_name)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    spec = dict(job.get("spec", {}))
    forge = spec.get("executionEngine", {}).get("forge", {})
    project = forge.get("project", "unknown")

    spec.pop("shutdown", None)

    new_name = sanitize_job_name(f"forge-{project}")
    body = {
        "apiVersion": f"{settings.fournos_api_group}/{settings.fournos_api_version}",
        "kind": "FournosJob",
        "metadata": {
            "name": new_name,
            "namespace": settings.fournos_namespace,
        },
        "spec": spec,
    }

    try:
        created = k8s_client.create_fournos_job(body)
        created_name = created.get("metadata", {}).get("name", new_name)
        return {"status": "ok", "job_name": created_name, "redirect": f"/jobs/{created_name}"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


async def _get_job_for_rerun(job_name: str) -> dict | None:
    """Fetch a FournosJob by name from live K8s or DB history."""
    live = k8s_client.get_fournos_job(job_name)
    if live:
        return live
    async with db.async_session() as session:
        db_job = await db.get_job_by_name(session, job_name)
        if db_job:
            return _db_job_to_fjob_dict(db_job)
    return None


@app.delete("/api/history/{job_name}")
async def delete_history_job(job_name: str):
    """Delete a job from the history database."""
    async with db.async_session() as session:
        async with session.begin():
            deleted = await db.delete_job_by_name(session, job_name)
    if not deleted:
        raise HTTPException(status_code=404, detail="Job not found in history")
    return {"status": "ok"}


@app.get("/api/jobs/{job_name}/logs/{pod_name}")
async def stream_logs(job_name: str, pod_name: str):
    """Stream live pod logs via SSE (only for running jobs)."""
    job_pods = k8s_client.list_pods_for_job(job_name)
    pod_names = {p["name"] for p in job_pods}
    if pod_name not in pod_names:
        raise HTTPException(status_code=404, detail="Pod not found for this job")

    async def generate():
        for line in k8s_client.read_pod_log(pod_name, follow=True):
            yield f"data: {line}\n\n"
            await asyncio.sleep(0.01)

    return StreamingResponse(generate(), media_type="text/event-stream")




# ---------------------------------------------------------------------------
# Routes: Submit Job
# ---------------------------------------------------------------------------

@app.get("/submit", response_class=HTMLResponse)
async def submit_form(request: Request):
    projects = discover_projects()
    return _render(
        "submit_job.html",
        projects=projects,
        pipelines=list(settings.default_pipelines),
    )


@app.get("/api/project-info/{project_name}")
async def project_info_api(project_name: str):
    from app.forge_discovery import get_project
    proj = get_project(project_name)
    if proj is None:
        return {"presets": [], "cluster": ""}
    return {"presets": proj.presets, "cluster": proj.cluster}


@app.post("/submit")
async def submit_job(
    request: Request,
    project: str = Form(...),
    cluster: str = Form(...),
    pipeline: str = Form("forge-test-only"),
    preset: str = Form(""),
    version: str = Form(""),
    owner: str = Form(""),
    exclusive: str = Form("false"),
    config_overrides_raw: str = Form(""),
):
    exclusive_bool = exclusive.lower() in ("true", "on", "1", "yes")

    config_overrides: dict[str, Any] = {}
    if config_overrides_raw.strip():
        for line in config_overrides_raw.strip().splitlines():
            line = line.strip()
            if ":" in line:
                k, v = line.split(":", 1)
                config_overrides[k.strip()] = v.strip()

    if version:
        version_key = _get_version_config_key(project)
        config_overrides[version_key] = version

    args = [preset] if preset else []

    job_name = sanitize_job_name(f"forge-{project}")

    body = {
        "apiVersion": f"{settings.fournos_api_group}/{settings.fournos_api_version}",
        "kind": "FournosJob",
        "metadata": {
            "name": job_name,
            "namespace": settings.fournos_namespace,
        },
        "spec": {
            "cluster": cluster,
            "displayName": f"{project} {preset}".strip(),
            "owner": owner or "fournos-dashboard",
            "pipeline": pipeline,
            "exclusive": exclusive_bool,
            "executionEngine": {
                "forge": {
                    "project": project,
                    "args": args,
                    "configOverrides": config_overrides,
                }
            },
        },
    }

    try:
        created = k8s_client.create_fournos_job(body)
        created_name = created.get("metadata", {}).get("name", job_name)

        async with db.async_session() as session:
            async with session.begin():
                await db.upsert_job(
                    session,
                    name=created_name,
                    project=project,
                    preset=preset,
                    cluster=cluster,
                    pipeline=pipeline,
                    owner=owner or "fournos-dashboard",
                    status="Pending",
                    config_overrides=config_overrides,
                    fjob_spec=body.get("spec", {}),
                )

        return RedirectResponse(url=f"/jobs/{created_name}", status_code=303)
    except Exception as exc:
        projects = discover_projects()
        return _render(
            "submit_job.html",
            projects=projects,
            pipelines=list(settings.default_pipelines),
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Routes: Schedules
# ---------------------------------------------------------------------------

@app.get("/schedules", response_class=HTMLResponse)
async def schedules_list(request: Request):
    cronjobs = k8s_client.list_managed_cronjobs()
    projects = discover_projects()
    return _render(
        "schedules.html",
        cronjobs=cronjobs,
        projects=projects,
        pipelines=list(settings.default_pipelines),
    )


@app.get("/schedules/{name}/runs", response_class=HTMLResponse)
async def schedule_runs(request: Request, name: str):
    """Show all jobs triggered by a specific schedule."""
    async with db.async_session() as session:
        jobs = await db.list_jobs_by_schedule(session, name)
    runs = []
    for j in jobs:
        runs.append({
            "name": j.name,
            "status": j.status,
            "preset": j.preset,
            "trigger_type": j.trigger_type or "scheduled",
            "duration_seconds": j.duration_seconds,
            "mlflow_url": j.mlflow_url,
            "created_at": j.created_at.isoformat() if j.created_at else "",
        })
    return _render("schedule_runs.html", schedule_name=name, runs=runs)


@app.post("/schedules")  # handles both create and edit
async def create_schedule(
    request: Request,
    name: str = Form(...),
    project: str = Form(...),
    cluster: str = Form(...),
    pipeline: str = Form("forge-test-only"),
    preset: str = Form(""),
    cron_expr: str = Form(...),
    image_source: str = Form(""),
    owner: str = Form(""),
    resolver_script: str = Form(""),
    resolver_image: str = Form(""),
    resolver_filename: str = Form(""),
    edit_target: str = Form(""),
):
    try:
        if edit_target:
            try:
                k8s_client.delete_cronjob(edit_target)
            except Exception:
                pass

        k8s_client.create_cronjob(
            name=name,
            schedule=cron_expr,
            project=project,
            cluster=cluster,
            pipeline=pipeline,
            preset=preset,
            image=image_source,
            owner=owner,
            resolver_script=resolver_script.strip().replace("\r\n", "\n").replace("\r", "\n"),
            resolver_image=resolver_image.strip(),
            resolver_filename=resolver_filename.strip(),
        )
        return RedirectResponse(url="/schedules", status_code=303)
    except Exception as exc:
        cronjobs = k8s_client.list_managed_cronjobs()
        projects = discover_projects()
        return _render(
            "schedules.html",
            cronjobs=cronjobs,
            projects=projects,
            pipelines=list(settings.default_pipelines),
            error=str(exc),
        )


@app.post("/api/schedules/{name}/toggle")
async def toggle_schedule(name: str):
    cj = k8s_client.get_managed_cronjob(name)
    if cj is None:
        raise HTTPException(404, "Schedule not found")
    k8s_client.patch_cronjob_suspend(name, suspend=not cj["suspend"])
    return {"status": "ok"}


@app.get("/api/schedules/{name}/resolver")
async def get_resolver_script(name: str):
    """Return the resolver script content for a schedule."""
    cj = k8s_client.get_managed_cronjob(name)
    if cj is None:
        raise HTTPException(404, "Schedule not found")
    cm_name = cj.get("resolver_configmap", "")
    if not cm_name:
        raise HTTPException(404, "No resolver script configured for this schedule")
    filename, content = k8s_client.get_resolver_script(cm_name)
    if not content:
        raise HTTPException(404, "Resolver ConfigMap not found")
    return {"filename": filename, "content": content}


@app.post("/api/schedules/{name}/trigger")
async def trigger_schedule(name: str):
    """Manually trigger a CronJob by creating a one-off Job from it."""
    try:
        job = k8s_client.trigger_cronjob(name)
        return {"status": "ok", "job_name": job}
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/api/schedules/{name}/delete")
async def delete_schedule(name: str):
    try:
        k8s_client.delete_cronjob(name)
        return {"status": "ok"}
    except Exception as exc:
        raise HTTPException(500, str(exc))


# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------

def _collect_clusters(live_jobs: list[dict]) -> list[str]:
    """Collect unique cluster names from live jobs."""
    clusters = set()
    for j in live_jobs:
        c = j.get("spec", {}).get("cluster", "")
        if c:
            clusters.add(c)
    return sorted(clusters)


def _db_job_to_dict(job: db.Job) -> dict:
    """Convert a DB Job row to a dict suitable for the history table template."""
    return {
        "name": job.name,
        "project": job.project,
        "preset": job.preset,
        "cluster": job.cluster,
        "pipeline": job.pipeline,
        "owner": job.owner,
        "phase": job.status,
        "message": job.message,
        "created_at": job.created_at.isoformat() if job.created_at else "",
        "completed_at": job.completed_at.isoformat() if job.completed_at else "",
        "duration_seconds": job.duration_seconds,
        "mlflow_url": job.mlflow_url,
        "error_message": job.error_message,
        "triggered_by_schedule": job.triggered_by_schedule,
        "trigger_type": job.trigger_type or "manual",
        "source": "history",
    }


def _db_job_to_fjob_dict(job: db.Job) -> dict:
    """Convert a DB Job row to a FournosJob-like dict for the detail template."""
    spec = job.fjob_spec or {}
    status = job.fjob_status or {}

    forge = spec.get("executionEngine", {}).get("forge", {})
    if not forge:
        forge = {"project": job.project, "args": job.preset.split() if job.preset else [], "configOverrides": job.config_overrides or {}}
        spec.setdefault("executionEngine", {})["forge"] = forge

    spec.setdefault("cluster", job.cluster)
    spec.setdefault("pipeline", job.pipeline)
    spec.setdefault("owner", job.owner)
    spec.setdefault("displayName", f"{job.project} {job.preset}".strip())
    spec.setdefault("exclusive", True)
    spec.setdefault("env", {})
    spec.setdefault("secretRefs", [])

    status.setdefault("phase", job.status)
    status.setdefault("message", job.message)
    status.setdefault("conditions", [])

    return {
        "metadata": {
            "name": job.name,
            "namespace": settings.fournos_namespace,
            "creationTimestamp": job.created_at.isoformat() if job.created_at else "",
            "uid": job.id,
        },
        "spec": spec,
        "status": status,
        "_source": "history",
        "_duration_seconds": job.duration_seconds,
        "_mlflow_url": job.mlflow_url,
        "_ci_artifacts_url": job.ci_artifacts_url,
    }


_PROJECT_VERSION_KEYS: dict[str, str] = {
    "mcp_gateway": "infrastructure.mcp_gateway_version",
}


def _get_version_config_key(project: str) -> str:
    """Return the configOverrides key used to pass the version for a project."""
    return _PROJECT_VERSION_KEYS.get(project, "infrastructure.version")


def sanitize_job_name(prefix: str) -> str:
    """Generate a K8s-safe job name with timestamp."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = f"{prefix}-{ts}".lower()
    name = re.sub(r"[^a-z0-9-]", "-", name)
    name = re.sub(r"-+", "-", name).strip("-")
    return name[:63]


