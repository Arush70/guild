"""Trust in a project's own codeguild configuration.

`.codeguild/config.yaml` names commands codeguild runs (the test command, the lint command,
the sandbox), and `.codeguild/roles/` and `.codeguild/profiles/` decide what the agents are
told and which models are called. These files may be committed, so a team shares them — and
so a clone of someone else's project brings its own "test command" along.

A folder whose configuration is tracked by git must therefore be trusted once on this machine
(`codeguild trust`) before any job runs in it. Setting a project up here (`codeguild init`,
or the dashboard creating the config) trusts it, because then the owner wrote the config.
Like an editor's workspace trust, it is given per folder and stays until withdrawn.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import ConfigError
from .paths import GUILD_DIR, atomic_write_text
from .repo import Repo

# what in .codeguild/ changes codeguild's behaviour (plans, traces and chat do not)
_CONFIG_FILES = ("config.yaml", "prices.yaml")
_CONFIG_DIRS = ("roles", "profiles")


class UntrustedProject(ConfigError):
    """The repository ships codeguild configuration that the owner has not approved here."""


def shipped_config(root: Path) -> list[str]:
    """Files under .codeguild/ that came with the repository (tracked by git) and steer us."""
    tracked = Repo(root).out("ls-files", "-z", "--", GUILD_DIR).split("\0")
    out = []
    for rel in filter(None, tracked):
        parts = Path(rel).parts[1:]
        if parts and (parts[0] in _CONFIG_FILES or parts[0] in _CONFIG_DIRS):
            out.append(rel)
    return sorted(out)


def _store() -> Path:
    return Path.home() / GUILD_DIR / "trusted.json"


def _trusted() -> dict:
    try:
        data = json.loads(_store().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def needs_trust(root: Path) -> list[str]:
    """The configuration files that came with the repository, when the owner has not trusted
    this folder yet; empty when there are none or the folder is trusted."""
    if str(root.resolve()) in _trusted():
        return []
    return shipped_config(root)


def trust(root: Path) -> None:
    """Record that the owner accepts this folder's codeguild configuration."""
    trusted = _trusted()
    trusted[str(root.resolve())] = True
    atomic_write_text(_store(), json.dumps(trusted, indent=2))


def require_trust(root: Path) -> None:
    files = needs_trust(root)
    if files:
        raise UntrustedProject(
            "this repository brings its own codeguild configuration "
            f"({', '.join(files[:4])}{', …' if len(files) > 4 else ''}), which decides what "
            "commands codeguild runs here. Read it, then run `codeguild trust` in this folder."
        )
