"""Where agent commands run.

- LocalSandbox: subprocess in the project dir. Fast; trusts the code being worked on.
- DockerSandbox: same command inside a container with the project bind-mounted, no network.
  Use this when running code you did not write (e.g. the Engineer's fresh changes).

Both strip API keys from the environment so a model can never read them via `env`.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .errors import Cancelled
from .paths import PROTECTED_DIRS

SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


@dataclass
class RunResult:
    returncode: int
    output: str
    timed_out: bool = False


def _clean_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not any(h in k.upper() for h in SECRET_HINTS)}
    # Child processes (pytest, ruff, the model's commands) print UTF-8 regardless of the console
    # code page; on Windows the default cp1252 pipe would otherwise mangle or crash on "—" / "✓".
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def _run(
    command: str | list[str],
    *,
    cwd: Path | None,
    timeout: int,
    cancel: threading.Event,
    stop: Callable[[subprocess.Popen], None],
    startup: int = 0,
) -> RunResult:
    """Run a command with a time limit; `stop(proc)` must end it and everything it started.
    `startup` is extra time that does not count against the command (starting a container).
    Raises Cancelled when `cancel` is set while it runs."""
    # Popen + its own process group, so the whole tree can be killed. With subprocess.run() on
    # Windows, kill() only ends cmd.exe; the python/pytest grandchild keeps the pipe open and
    # communicate() blocks forever — guild would hang on an infinite-loop test.
    group: dict = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    proc = subprocess.Popen(
        command,
        shell=isinstance(command, str),
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=_clean_env(),
        encoding="utf-8",
        errors="replace",
        **group,
    )
    deadline = time.monotonic() + timeout + startup
    while True:
        try:
            # short waits so a cancel (owner pressed stop) ends a 10-minute test run now
            out, _ = proc.communicate(timeout=max(0.05, min(0.5, deadline - time.monotonic())))
            return RunResult(proc.returncode, out or "")
        except subprocess.TimeoutExpired:
            if not cancel.is_set() and time.monotonic() < deadline:
                continue
            stop(proc)
            try:
                out, _ = proc.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                out = ""
            if cancel.is_set():
                raise Cancelled() from None
            return RunResult(124, f"{out or ''}\n[timed out after {timeout}s]", timed_out=True)


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except OSError:  # already gone, or no such group
        proc.kill()


class LocalSandbox:
    def __init__(self, root: Path, cancel: threading.Event | None = None):
        self.root = root
        self.cancel = cancel or threading.Event()

    def run(self, command: str, timeout: int = 120) -> RunResult:
        return _run(command, cwd=self.root, timeout=timeout, cancel=self.cancel, stop=_kill_tree)


class DockerSandbox:
    def __init__(self, root: Path, image: str, cancel: threading.Event | None = None):
        self.root = root
        self.image = image
        self.cancel = cancel or threading.Event()
        if shutil.which("docker") is None:
            raise RuntimeError("docker not found on PATH; set sandbox: local in .guild/config.yaml")

    def run(self, command: str, timeout: int = 120) -> RunResult:
        name = f"guild-{uuid.uuid4().hex[:12]}"
        args = [
            "docker",
            "run",
            "--rm",
            "--name",
            name,
            "--network",
            "none",
            "--memory",
            "2g",
            "--cpus",
            "2",
            "-v",
            f"{self.root.resolve()}:/work",
            "-w",
            "/work",
        ]
        # .git and .guild are mounted read-only on top of the project: a command in the
        # container must not plant a git hook or edit git/guild config, because git and guild
        # then run on the host — outside the sandbox — and would execute it.
        for protected in sorted(PROTECTED_DIRS):
            if (self.root / protected).is_dir():
                args += ["-v", f"{(self.root / protected).resolve()}:/work/{protected}:ro"]
        args += [self.image, "sh", "-c", command]

        def stop(proc: subprocess.Popen) -> None:
            # killing the docker client leaves the container running; end the container
            subprocess.run(["docker", "kill", name], capture_output=True, check=False)
            _kill_tree(proc)

        return _run(args, cwd=None, timeout=timeout, cancel=self.cancel, stop=stop, startup=15)


def make_sandbox(root: Path, kind: str, image: str, cancel: threading.Event | None = None):
    if kind == "docker":
        return DockerSandbox(root, image, cancel)
    if kind != "local":
        raise ValueError(f"unknown sandbox {kind!r} (use 'local' or 'docker')")
    return LocalSandbox(root, cancel)
