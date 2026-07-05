"""Discover Forge projects, presets, and config schemas from the repo."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from app.config import settings
from app.models import ProjectInfo

logger = logging.getLogger(__name__)

_cache: dict[str, ProjectInfo] | None = None


def _forge_projects_dir() -> Path | None:
    """Resolve the forge projects/ directory."""
    if settings.forge_repo_path:
        p = Path(settings.forge_repo_path) / "projects"
        if p.is_dir():
            return p
    return None


def discover_projects(force_refresh: bool = False) -> list[ProjectInfo]:
    """Discover projects from Forge repo or ConfigMap-backed YAML file."""
    global _cache
    if _cache is not None and not force_refresh:
        return list(_cache.values())

    result: dict[str, ProjectInfo] = {}

    projects_dir = _forge_projects_dir()
    if projects_dir is not None:
        result = _discover_from_repo(projects_dir)

    if not result:
        result = _discover_from_configmap()

    _cache = result
    logger.info("Discovered %d Forge projects", len(result))
    return list(result.values())


def _discover_from_repo(projects_dir: Path) -> dict[str, ProjectInfo]:
    """Scan the Forge repo for available projects."""
    result: dict[str, ProjectInfo] = {}
    skip = {"core", "__pycache__"}

    for proj_dir in sorted(projects_dir.iterdir()):
        if not proj_dir.is_dir() or proj_dir.name.startswith(".") or proj_dir.name in skip:
            continue

        orchestration = proj_dir / "orchestration"
        if not orchestration.is_dir():
            continue

        presets = _load_presets(orchestration)
        config_keys = _load_config_keys(orchestration)
        has_cli = (orchestration / "cli.py").exists()

        result[proj_dir.name] = ProjectInfo(
            name=proj_dir.name,
            presets=presets,
            config_keys=config_keys,
            has_cli=has_cli,
        )

    return result


def _discover_from_configmap() -> dict[str, ProjectInfo]:
    """Load project definitions from a YAML config file (mounted from a ConfigMap)."""
    config_path = Path(settings.projects_config_path)
    if not config_path.exists():
        logger.warning("No projects config at %s", config_path)
        return {}

    try:
        with open(config_path) as f:
            data = yaml.safe_load(f)
    except Exception as exc:
        logger.error("Failed to parse projects config: %s", exc)
        return {}

    if not isinstance(data, dict) or "projects" not in data:
        logger.warning("Projects config missing 'projects' key")
        return {}

    result: dict[str, ProjectInfo] = {}
    for proj in data["projects"]:
        name = proj.get("name", "")
        if not name:
            continue
        result[name] = ProjectInfo(
            name=name,
            cluster=proj.get("cluster", ""),
            presets=proj.get("presets", []),
            config_keys=proj.get("config_keys", []),
            has_cli=proj.get("has_cli", False),
        )

    logger.info("Loaded %d projects from ConfigMap", len(result))
    return result


def get_project(name: str) -> ProjectInfo | None:
    """Get info for a specific project."""
    projects = discover_projects()
    return next((p for p in projects if p.name == name), None)


def get_project_presets(name: str) -> list[str]:
    """Get available presets for a project."""
    proj = get_project(name)
    return proj.presets if proj else []


def _load_presets(orchestration_dir: Path) -> list[str]:
    """Load preset names from presets.d/."""
    presets_dir = orchestration_dir / "presets.d"
    if not presets_dir.is_dir():
        return []

    preset_names: list[str] = []
    for yaml_file in sorted(presets_dir.glob("*.yaml")):
        try:
            with open(yaml_file) as f:
                data = yaml.safe_load(f)
            if isinstance(data, dict):
                preset_names.extend(data.keys())
        except Exception as exc:
            logger.debug("Failed to parse %s: %s", yaml_file, exc)
    return preset_names


def _load_config_keys(orchestration_dir: Path) -> list[str]:
    """Extract top-level config keys from config.yaml and config.d/."""
    keys: list[str] = []

    config_file = orchestration_dir / "config.yaml"
    if config_file.exists():
        try:
            with open(config_file) as f:
                data = yaml.safe_load(f)
            if isinstance(data, dict):
                keys.extend(data.keys())
        except Exception:
            pass

    config_d = orchestration_dir / "config.d"
    if config_d.is_dir():
        for yaml_file in sorted(config_d.glob("*.yaml")):
            keys.append(yaml_file.stem)

    return keys
