"""Where agent commands run.

- LocalSandbox: subprocess in the project dir. Fast; trusts the code being worked on.
- DockerSandbox: same command inside a container with the project bind-mounted, no network.
  Use this when running code you did not write (e.g. the Engineer's fresh changes).

Both strip API keys from the environment so a model can never read them via `env`.
"""
from __future__ import annotations

import os
import shutil
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
    return {k: v for k, v in os.environ.items() if not any(h in k.upper() for h in SECRET_HINTS)}


class LocalSandbox:
    kind = "local"

    def __init__(self, root: Path):
        self.root = root

    def run(self, command: str, timeout: int = 120) -> RunResult:
        try:
            r = subprocess.run(command, shell=True, cwd=self.root, capture_output=True, text=True,
                               timeout=timeout, env=_clean_env())
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            return RunResult(124, f"{out}\n[timed out after {timeout}s]", timed_out=True)
        return RunResult(r.returncode, r.stdout + r.stderr)


class DockerSandbox:
    kind = "docker"

    def __init__(self, root: Path, image: str = "python:3.11-slim", network: bool = False):
        self.root = root
        self.image = image
        self.network = network
        if shutil.which("docker") is None:
            raise RuntimeError("docker not found on PATH; set sandbox: local in .guild/config.yaml")

    def run(self, command: str, timeout: int = 120) -> RunResult:
        args = ["docker", "run", "--rm", "-v", f"{self.root.resolve()}:/work", "-w", "/work",
                "--memory", "2g", "--cpus", "2"]
        if not self.network:
            args += ["--network", "none"]
        args += [self.image, "sh", "-c", command]
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=timeout + 15, env=_clean_env())
        except subprocess.TimeoutExpired:
            return RunResult(124, f"[timed out after {timeout}s]", timed_out=True)
        return RunResult(r.returncode, r.stdout + r.stderr)


def make_sandbox(root: Path, kind: str, image: str = "python:3.11-slim"):
    if kind == "docker":
        return DockerSandbox(root, image)
    return LocalSandbox(root)
