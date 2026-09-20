"""Configuration: profiles (which models fill which slots) and roles (who does what).

Resolution order for both profiles and roles:
  1. <project>/.guild/profiles/<name>.yaml  or  <project>/.guild/roles/<name>.yaml
  2. built-in package data (src/guild/data/...)

So users can override anything by dropping a YAML file into .guild/ without forking.
"""
from __future__ import annotations

import os
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

GUILD_DIR = ".guild"


class Limits(BaseModel):
    max_revision_rounds: int = 3
    escalate_after: int | None = None  # None = never escalate
    max_tool_calls_per_task: int = 60
    max_usd_per_run: float = 0.0


class Profile(BaseModel):
    name: str
    description: str = ""
    monthly_budget_gbp: float | None = 0
    slots: dict[str, list[str]]
    limits: Limits = Field(default_factory=Limits)

    @field_validator("slots")
    @classmethod
    def _non_empty(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        for k, chain in v.items():
            if not chain:
                raise ValueError(f"slot '{k}' has no candidates")
        return v

    def chain(self, slot: str) -> list[str]:
        if slot not in self.slots:
            raise KeyError(f"profile '{self.name}' has no slot '{slot}'")
        return self.slots[slot]


class Role(BaseModel):
    name: str
    title: str
    slot: str
    escalation_slot: str | None = None
    tools: list[str] = Field(default_factory=list)
    max_tool_calls: int = 30
    write_allowlist: list[str] | None = None
    description: str = ""
    system_prompt: str


class ProjectConfig(BaseModel):
    """Per-project settings stored in <project>/.guild/config.yaml."""

    profile: str = "free"
    test_command: str = "python -m pytest -q"
    lint_command: str | None = None
    sandbox: str = "local"  # "local" | "docker"
    docker_image: str = "python:3.11-slim"
    ignore: list[str] = Field(
        default_factory=lambda: [".git", ".guild", "node_modules", ".venv", "venv",
                                 "__pycache__", "*.pyc", "dist", "build", ".mypy_cache"]
    )
    roles_enabled: list[str] = Field(
        default_factory=lambda: ["lead", "engineer", "verifier", "critic", "security", "docs"]
    )


# ---------------------------------------------------------------------------


def _package_data(sub: str) -> Path:
    return Path(str(resources.files("guild").joinpath("data", sub)))


def _load_yaml(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping")
    return data


def _find(kind: str, name: str, project: Path | None) -> Path:
    candidates: list[Path] = []
    if project is not None:
        candidates.append(project / GUILD_DIR / kind / f"{name}.yaml")
    candidates.append(_package_data(kind) / f"{name}.yaml")
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError(f"no {kind[:-1]} named '{name}' (looked in {[str(c) for c in candidates]})")


def load_profile(name: str, project: Path | None = None) -> Profile:
    return Profile.model_validate(_load_yaml(_find("profiles", name, project)))


def load_role(name: str, project: Path | None = None) -> Role:
    return Role.model_validate(_load_yaml(_find("roles", name, project)))


def list_available(kind: str, project: Path | None = None) -> list[str]:
    names: set[str] = set()
    for d in ([project / GUILD_DIR / kind] if project else []) + [_package_data(kind)]:
        if d.exists():
            names.update(p.stem for p in d.glob("*.yaml"))
    return sorted(names)


def load_project_config(project: Path) -> ProjectConfig:
    path = project / GUILD_DIR / "config.yaml"
    if not path.exists():
        return ProjectConfig()
    return ProjectConfig.model_validate(_load_yaml(path))


def save_project_config(project: Path, cfg: ProjectConfig) -> Path:
    d = project / GUILD_DIR
    d.mkdir(parents=True, exist_ok=True)
    path = d / "config.yaml"
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg.model_dump(), f, sort_keys=False)
    return path


def find_project_root(start: Path | None = None) -> Path:
    """Walk up until a directory containing .guild/ or .git/ is found; else cwd."""
    p = (start or Path.cwd()).resolve()
    for cand in [p, *p.parents]:
        if (cand / GUILD_DIR).is_dir() or (cand / ".git").is_dir():
            return cand
    return p


def env_flag(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}
