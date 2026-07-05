"""Pydantic models for the Fournos Dashboard."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ProjectInfo(BaseModel):
    """Discovered Forge project metadata."""

    name: str
    cluster: str = ""
    presets: list[str] = Field(default_factory=list)
    config_keys: list[str] = Field(default_factory=list)
    has_cli: bool = False
