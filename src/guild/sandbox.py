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
from dataclasses import dataclass
from pathlib import Path

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


class LocalSandbox:
    kind = "local"

    def __init__(self, root: Path):
        self.root = root

    def run(self, command: str, timeout: int = 120) -> RunResult:
        # Popen + process group so a timeout kills the whole tree. With subprocess.run() on
        # Windows, kill() only ends cmd.exe; the python/pytest grandchild keeps the pipe open
        # and communicate() blocks forever — guild would hang on an infinite-loop test.
        kwargs: dict = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=self.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=_clean_env(),
            encoding="utf-8",
            errors="replace",
            **kwargs,
        )
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            try:
                out, _ = proc.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                out = ""
            return RunResult(124, f"{out or ''}\n[timed out after {timeout}s]", timed_out=True)
        return RunResult(proc.returncode, out or "")


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        proc.kill()


class DockerSandbox:
    kind = "docker"

    def __init__(self, root: Path, image: str = "python:3.11-slim", network: bool = False):
        self.root = root
        self.image = image
        self.network = network
        if shutil.which("docker") is None:
            raise RuntimeError("docker not found on PATH; set sandbox: local in .guild/config.yaml")

    def run(self, command: str, timeout: int = 120) -> RunResult:
        args = [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{self.root.resolve()}:/work",
            "-w",
            "/work",
            "--memory",
            "2g",
            "--cpus",
            "2",
        ]
        if not self.network:
            args += ["--network", "none"]
        args += [self.image, "sh", "-c", command]
        try:
            r = subprocess.run(
                args,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout + 15,
                env=_clean_env(),
            )
        except subprocess.TimeoutExpired:
            return RunResult(124, f"[timed out after {timeout}s]", timed_out=True)
        return RunResult(r.returncode, r.stdout + r.stderr)


def make_sandbox(root: Path, kind: str, image: str = "python:3.11-slim"):
    if kind == "docker":
        return DockerSandbox(root, image)
    return LocalSandbox(root)
