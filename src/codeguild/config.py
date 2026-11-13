"""Configuration: profiles (which models fill which slots) and roles (who does what).

Resolution order for both profiles and roles:
  1. <project>/.codeguild/profiles/<name>.yaml  or  <project>/.codeguild/roles/<name>.yaml
  2. built-in package data (src/codeguild/data/...)

So users can override anything by dropping a YAML file into .codeguild/ without forking.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .paths import (
    CACHE_DIRS,
    PROTECTED_DIRS,
    ProjectPaths,
    adopt_legacy_project,
    atomic_write_text,
)

_M = TypeVar("_M", bound=BaseModel)


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
    # True: the tool budget is the profile's limits.max_tool_calls_per_task instead (the role
    # that implements tasks; cheap profiles give it less, strong ones more)
    task_budget: bool | None = None
    write_allowlist: list[str] | None = None
    readonly: bool | None = None  # None = infer from tools/name (reviewers are read-only)
    description: str = ""
    system_prompt: str

    @model_validator(mode="after")
    def _engineer_has_the_task_budget(self) -> Role:
        # role files written before `task_budget` existed (a project's own engineer.yaml) do
        # not have the line; the engineer always had the task budget
        if self.task_budget is None:
            self.task_budget = self.name == "engineer"
        return self


class ProjectConfig(BaseModel):
    """Per-project settings stored in <project>/.codeguild/config.yaml."""

    # also validate `cfg.sandbox = ...` style updates (the dashboard's /api/config does that)
    model_config = ConfigDict(validate_assignment=True)

    profile: str = "free"
    test_command: str = "python -m pytest -q"
    lint_command: str | None = None
    sandbox: str = "local"  # "local" | "docker"
    docker_image: str = "python:3.11-slim"
    ignore: list[str] = Field(
        default_factory=lambda: [
            *sorted(PROTECTED_DIRS),
            "node_modules",
            ".venv",
            "venv",
            *CACHE_DIRS,
            "*.pyc",
            "dist",
            "build",
        ]
    )
    roles_enabled: list[str] = Field(
        default_factory=lambda: ["lead", "engineer", "verifier", "critic", "security", "docs"]
    )

    @field_validator("sandbox", mode="before")
    @classmethod
    def _sandbox(cls, v: object) -> object:
        # A typo ("dockr") must never quietly mean "local": that runs untrusted code on the
        # host while the config claims isolation.
        if isinstance(v, str):
            v = v.strip().lower()
        if v not in {"local", "docker"}:
            raise ValueError(f"sandbox must be 'local' or 'docker', not {v!r}")
        return v


# ---------------------------------------------------------------------------


def _package_data(sub: str) -> Path:
    return Path(str(resources.files("codeguild").joinpath("data", sub)))


class ConfigError(ValueError):
    """A config, profile or role file that cannot be used. The message names the file and the
    first problem in one line, for a terminal or the dashboard."""


def _load(model: type[_M], path: Path) -> _M:
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            raise ValueError("must contain a mapping (key: value lines)")
        return model.model_validate(data)
    except ValidationError as e:
        first = e.errors()[0]
        where = ".".join(str(x) for x in first["loc"])
        raise ConfigError(f"{path}: {where}: {first['msg']}") from None
    except (yaml.YAMLError, UnicodeDecodeError, ValueError) as e:
        raise ConfigError(f"{path}: {' '.join(str(e).split())[:200]}") from None


def _find(kind: str, name: str, project: Path | None) -> Path:
    if not name or any(c in name for c in "/\\") or name.startswith("."):
        # names come from config files and the dashboard; never let one walk the filesystem
        raise FileNotFoundError(f"invalid {kind[:-1]} name: {name!r}")
    candidates: list[Path] = []
    if project is not None:
        candidates.append(ProjectPaths(project).overrides(kind) / f"{name}.yaml")
    candidates.append(_package_data(kind) / f"{name}.yaml")
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError(
        f"no {kind[:-1]} named '{name}' (looked in {[str(c) for c in candidates]})"
    )


def load_profile(name: str, project: Path | None = None) -> Profile:
    return _load(Profile, _find("profiles", name, project))


def load_role(name: str, project: Path | None = None) -> Role:
    return _load(Role, _find("roles", name, project))


def list_available(kind: str, project: Path | None = None) -> list[str]:
    names: set[str] = set()
    own = [ProjectPaths(project).overrides(kind)] if project else []
    for d in [*own, _package_data(kind)]:
        if d.exists():
            names.update(p.stem for p in d.glob("*.yaml"))
    return sorted(names)


def load_project_config(project: Path) -> ProjectConfig:
    path = ProjectPaths(project).config
    if not path.exists():
        return ProjectConfig()
    return _load(ProjectConfig, path)


def save_project_config(project: Path, cfg: ProjectConfig) -> Path:
    path = ProjectPaths(project).config
    atomic_write_text(path, yaml.safe_dump(cfg.model_dump(), sort_keys=False))
    return path


def find_project_root(start: Path | None = None) -> Path:
    """Walk up until a directory with .codeguild/config.yaml or .git/ is found; else `start`."""
    p = (start or Path.cwd()).resolve()
    home = Path.home().resolve()
    for cand in [p, *p.parents]:
        if cand == home and cand != p:
            break  # a folder under home is never "inside" the home directory as a project
        adopt_legacy_project(cand)  # a project from before the rename: .guild → .codeguild
        paths = ProjectPaths(cand)
        # config.yaml, not the bare directory: ~/.codeguild only holds the dashboard's recent list
        if paths.initialised or paths.has_git:
            return cand
    return p
