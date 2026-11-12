"""Where guild keeps its files, and the file names it treats specially (protected directories,
caches, credential files). Every path under `.guild/` is named here and nowhere else."""

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


# Files whose whole purpose is to hold credentials. The sandbox strips API keys from the
# environment; reading one of these would send them to the model provider and into the trace.
_SECRET_NAMES = frozenset(
    {".netrc", "_netrc", ".pypirc", ".npmrc", ".git-credentials", "id_rsa", "id_dsa", "id_ecdsa"}
    | {"id_ed25519", "credentials.json", "secrets.json", "secrets.yaml", "secrets.yml"}
)
_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore")
_ENV_TEMPLATES = (".example", ".sample", ".template", ".dist")


def windows_name(name: str) -> str:
    """The name Windows opens for `name`: it drops trailing dots and spaces and an NTFS stream
    suffix, and ignores case. ".env." , ".ENV" and ".env::$DATA" are all the file ".env"."""
    return name.split(":", 1)[0].rstrip(" .").lower()


def is_secret_file(name: str) -> bool:
    n = windows_name(Path(name).name)
    if n == ".env":
        return True
    if n.startswith(".env."):
        return not n.endswith(_ENV_TEMPLATES)
    return n in _SECRET_NAMES or n.endswith(_SECRET_SUFFIXES)


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

    @property
    def lock(self) -> Path:
        return self.guild_dir / "lock"

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


class ProjectBusy(RuntimeError):
    """Another guild process is changing this project."""


class ProjectLock:
    """One process at a time may plan, run tasks or pair in a project: each of them switches
    branches, edits the working tree and rewrites plan.json. Two dashboards on one folder (a
    terminal's `guild ui` and the VS Code extension's) would otherwise undo each other's work.

    The lock is an OS lock on `.guild/lock`, so it disappears with the process that holds it,
    however that process ends. It can be taken again by its holder (a run inside a run).
    """

    _OFFSET = 1 << 30  # lock a byte far past the text, so the text stays readable on Windows

    def __init__(self, path: Path):
        self.path = path
        self._file = None
        self._depth = 0

    def __enter__(self) -> ProjectLock:
        self._depth += 1
        if self._depth > 1:
            return self
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            f = open(self.path, "a+b")  # stays open until __exit__: the open file is the lock
        except OSError:
            return self  # no lock file possible (read-only folder): work without one
        try:
            _lock_file(f, self._OFFSET)
        except OSError:
            f.seek(0)
            holder = f.read(200).decode("utf-8", "replace").strip() or "another guild process"
            f.close()
            self._depth = 0
            raise ProjectBusy(
                f"{holder} is already working on this project; wait for it to finish or stop it"
            ) from None
        with contextlib.suppress(OSError):
            f.seek(0)
            f.truncate()
            started = time.strftime("%H:%M:%S")
            f.write(f"guild process {os.getpid()} (started {started})".encode())
            f.flush()
        self._file = f
        return self

    def __exit__(self, *exc: object) -> None:
        self._depth -= 1
        if self._depth == 0 and self._file is not None:
            with contextlib.suppress(OSError):
                self._file.close()  # closing releases the lock
            self._file = None


def _lock_file(f, offset: int) -> None:
    """Take an exclusive lock without waiting; OSError when another process holds it."""
    if os.name == "nt":
        import msvcrt

        f.seek(offset)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


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
