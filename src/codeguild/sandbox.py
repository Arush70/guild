"""Where agent commands run.

- LocalSandbox: subprocess in the project dir. Fast; trusts the code being worked on.
- DockerSandbox: same command inside a container with the project bind-mounted, no network.
  Use this when running code you did not write (e.g. the Engineer's fresh changes).

Both strip API keys from the environment so a model can never read them via `env`.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .errors import Cancelled
from .paths import PROTECTED_DIRS

SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH", "COOKIE")
# a connection string with a password in it: scheme://user:password@host
_URL_WITH_PASSWORD = re.compile(r"\w+://[^/\s:@]*:[^/\s@]+@")
# kept although they match: commands need them (X11 display access, package downloads)
_NOT_SECRET = frozenset({"XAUTHORITY", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"})


@dataclass
class RunResult:
    returncode: int
    output: str
    timed_out: bool = False


def _clean_env() -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k.upper() in _NOT_SECRET
        or not (
            any(h in k.upper() for h in SECRET_HINTS)
            or k.upper().endswith(("_PAT", "_DSN"))
            or _URL_WITH_PASSWORD.search(v)
        )
    }
    # Child processes (pytest, ruff, the model's commands) print UTF-8 regardless of the console
    # code page; on Windows the default cp1252 pipe would otherwise mangle or crash on "—" / "✓".
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONUNBUFFERED", "1")
    # No .pyc files: Python trusts one when the source has the same size and the same
    # modification second, so an edit made right after a test run ("+" to "-") could be
    # tested as the old code.
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
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
    # communicate() blocks forever — codeguild would hang on an infinite-loop test.
    group: dict = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    proc = subprocess.Popen(
        command,
        shell=isinstance(command, str),
        cwd=cwd,
        stdin=subprocess.DEVNULL,  # a command that asks a question gets "no input", at once
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=_clean_env(),
        **group,
    )
    output = _Capture(proc.stdout)
    deadline = time.monotonic() + timeout + startup
    timed_out = False
    while True:
        try:
            # short waits so a cancel (owner pressed stop) ends a 10-minute test run now
            proc.wait(timeout=max(0.02, min(0.2, deadline - time.monotonic())))
            break
        except subprocess.TimeoutExpired:
            if cancel.is_set() or time.monotonic() >= deadline:
                timed_out = True
                stop(proc)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=15)
                break
    # The command is over. Something it started may still hold the output pipe open (a server
    # left in the background): give the last output a moment, then end what is left.
    output.join(timeout=1.0)
    if output.is_alive():
        stop(proc)
        output.join(timeout=2.0)
    if cancel.is_set():
        raise Cancelled() from None
    if timed_out:
        return RunResult(124, f"{output.text()}\n[timed out after {timeout}s]", timed_out=True)
    return RunResult(proc.returncode, output.text())


class _Capture(threading.Thread):
    """Reads a command's output as it comes and keeps the first and the last part of it. A
    test that prints in an endless loop must not fill codeguild's memory before its timeout."""

    KEEP = 1_000_000  # bytes at each end; tools show far less than this to a model

    def __init__(self, stream):
        super().__init__(daemon=True)
        self._stream = stream
        self._head = bytearray()
        self._tail: deque[bytes] = deque()
        self._tail_size = 0
        self._dropped = 0
        self.start()

    def run(self) -> None:
        fd = self._stream.fileno()
        with contextlib.suppress(OSError, ValueError):
            while chunk := os.read(fd, 65536):
                room = self.KEEP - len(self._head)
                if room > 0:
                    self._head += chunk[:room]
                    chunk = chunk[room:]
                if chunk:
                    self._tail.append(chunk)
                    self._tail_size += len(chunk)
                    while self._tail_size - len(self._tail[0]) >= self.KEEP:
                        gone = self._tail.popleft()
                        self._tail_size -= len(gone)
                        self._dropped += len(gone)

    def text(self) -> str:
        tail = b"".join(self._tail)
        dropped = self._dropped + max(0, len(tail) - self.KEEP)
        gap = f"\n[... {dropped} bytes of output left out ...]\n".encode() if dropped else b""
        text = (bytes(self._head) + gap + tail[-self.KEEP :]).decode("utf-8", errors="replace")
        return text.replace("\r\n", "\n").replace("\r", "\n")  # as text-mode pipes did


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False
            )
        else:
            # the group id is the pid (start_new_session); asking the OS for it fails once
            # the command itself has exited, while its children are still there
            os.killpg(proc.pid, signal.SIGKILL)
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
            raise RuntimeError(
                "docker not found on PATH; set sandbox: local in .codeguild/config.yaml"
            )

    def run(self, command: str, timeout: int = 120) -> RunResult:
        name = f"codeguild-{uuid.uuid4().hex[:12]}"
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
        # .git and .codeguild are mounted read-only on top of the project: a command in the
        # container must not plant a git hook or edit git/codeguild config: git and codeguild
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
