"""Trust in a project's own guild configuration.

`.guild/config.yaml` names commands guild runs (the test command, the lint command, the
sandbox), and `.guild/roles/` and `.guild/profiles/` decide what the agents are told and which
models are called. `guild init` keeps `.guild/` out of git, so normally the owner wrote these
files on this machine. When they are *tracked by git*, they arrived with the repository: a
clone of someone else's project would otherwise run that project's "test command" the first
time guild is used in it.

Such a project must be trusted once (`guild trust`). The trust is for the files as they are:
after they change (a `git pull`), guild asks again.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from pathlib import Path

from .config import ConfigError
from .paths import GUILD_DIR, atomic_write_text
from .repo import Repo

# what in .guild/ changes guild's behaviour (plans, traces and chat do not)
_CONFIG_FILES = ("config.yaml", "prices.yaml")
_CONFIG_DIRS = ("roles", "profiles")


class UntrustedProject(ConfigError):
    """The repository ships guild configuration that the owner has not approved here."""


def shipped_config(root: Path) -> list[str]:
    """Files under .guild/ that came with the repository (tracked by git) and steer guild."""
    tracked = Repo(root).out("ls-files", "-z", "--", GUILD_DIR).split("\0")
    out = []
    for rel in filter(None, tracked):
        parts = Path(rel).parts[1:]
        if parts and (parts[0] in _CONFIG_FILES or parts[0] in _CONFIG_DIRS):
            out.append(rel)
    return sorted(out)


def _fingerprint(root: Path, files: list[str]) -> str:
    digest = hashlib.sha256()
    for rel in files:
        digest.update(rel.encode())
        with contextlib.suppress(OSError):
            digest.update((root / rel).read_bytes())
    return digest.hexdigest()


def _store() -> Path:
    return Path.home() / GUILD_DIR / "trusted.json"


def _trusted() -> dict[str, str]:
    try:
        data = json.loads(_store().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def needs_trust(root: Path) -> list[str]:
    """The shipped configuration files the owner has not approved in their current state;
    empty when there are none or they are trusted."""
    files = shipped_config(root)
    if not files or _trusted().get(str(root.resolve())) == _fingerprint(root, files):
        return []
    return files


def trust(root: Path) -> None:
    """Record that the owner has read this project's shipped configuration as it is now."""
    trusted = _trusted()
    trusted[str(root.resolve())] = _fingerprint(root, shipped_config(root))
    atomic_write_text(_store(), json.dumps(trusted, indent=2))


def require_trust(root: Path) -> None:
    files = needs_trust(root)
    if files:
        raise UntrustedProject(
            "this repository brings its own guild configuration "
            f"({', '.join(files[:4])}{', …' if len(files) > 4 else ''}), which decides what "
            "commands guild runs here. Read it, then run `guild trust` in this folder."
        )
