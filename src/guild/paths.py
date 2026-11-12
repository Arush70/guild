"""Where guild keeps its files. Every path under `.guild/` is named here and nowhere else."""

from __future__ import annotations

import contextlib
import os
import time
from dataclasses import dataclass
from pathlib import Path

GUILD_DIR = ".guild"

# .git holds hooks and config that git itself executes; .guild holds guild's own config, roles,
# plan and traces. No agent may write to either, and the Docker sandbox mounts them read-only.
PROTECTED_DIRS = frozenset({".git", GUILD_DIR})

# Created as a side effect of running tests and linters: never committed, never shown to a model.
CACHE_DIRS = ("__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache")


@dataclass(frozen=True)
class ProjectPaths:
    root: Path

    @property
    def guild_dir(self) -> Path:
        return self.root / GUILD_DIR

    @property
    def config(self) -> Path:
        return self.guild_dir / "config.yaml"

    @property
    def plan(self) -> Path:
        return self.guild_dir / "plan.json"

    @property
    def chat(self) -> Path:
        return self.guild_dir / "chat.jsonl"

    @property
    def prices(self) -> Path:
        return self.guild_dir / "prices.yaml"

    @property
    def runs(self) -> Path:
        return self.guild_dir / "runs"

    def overrides(self, kind: str) -> Path:
        """Project-level `roles/` or `profiles/` that shadow the built-in ones."""
        return self.guild_dir / kind

    def trace(self, run_id: str) -> Path:
        return self.runs / run_id / "trace.jsonl"

    def traces(self) -> list[Path]:
        """Trace files of all runs, newest first."""

        def mtime(p: Path) -> float:
            try:
                return p.stat().st_mtime
            except OSError:  # run folder removed while listing
                return 0.0

        return sorted(self.runs.glob("*/trace.jsonl"), key=mtime, reverse=True)

    @property
    def initialised(self) -> bool:
        return self.config.exists()

    @property
    def has_git(self) -> bool:
        return (self.root / ".git").is_dir()

    @staticmethod
    def recent_projects() -> Path:
        """Per-user list of projects the dashboard has opened."""
        return Path.home() / GUILD_DIR / "recent.json"


def atomic_write_text(path: Path, text: str) -> None:
    """Replace `path` in one step, so a reader never sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(6):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            # Windows refuses the replace while another process has the target open (the
            # dashboard polls plan.json); the reader is done within milliseconds.
            if attempt == 5:
                with contextlib.suppress(OSError):
                    tmp.unlink()
                raise
            time.sleep(0.02)
