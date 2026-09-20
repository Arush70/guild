"""A scripted fake provider so the whole workflow can be tested without any API."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable

import pytest

from guild.config import Profile, ProjectConfig
from guild.providers.base import Completion, Message, ProviderError, ToolCall, Usage

Script = Callable[[str, list[Message]], Completion]  # (model_name, messages) -> completion


class FakeProvider:
    """Answers by role. Each role gets a list of turns; a turn is either a dict (final JSON)
    or a list of (tool_name, args) tool calls."""

    def __init__(self, turns_by_role: dict[str, list], fail_models: set[str] | None = None):
        self.turns = {k: list(v) for k, v in turns_by_role.items()}
        self.fail_models = fail_models or set()
        self.calls: list[tuple[str, str]] = []
        self._n = 0

    def complete(self, model_name, messages, tools, max_tokens, temperature):
        if model_name in self.fail_models:
            raise ProviderError(f"fake/{model_name}", "simulated outage", retryable=True)
        role = _role_of(messages)
        self.calls.append((role, model_name))
        queue = self.turns.get(role, [])
        turn = queue.pop(0) if queue else {"status": "done", "summary": "default"}
        self._n += 1
        if isinstance(turn, list):
            calls = [ToolCall(id=f"c{self._n}_{i}", name=n, arguments=a) for i, (n, a) in enumerate(turn)]
            msg = Message("assistant", "", tool_calls=calls)
        else:
            msg = Message("assistant", json.dumps(turn))
        return Completion(msg, Usage(100, 50), f"fake/{model_name}", "stop")


def _role_of(messages: list[Message]) -> str:
    sys = messages[0].content
    for r in ("Project Lead", "Software Engineer", "Critic", "Verifier", "Security", "Technical Writer", "Researcher"):
        if r in sys[:300]:
            return {"Project Lead": "lead", "Software Engineer": "engineer", "Critic": "critic",
                    "Verifier": "verifier", "Security": "security", "Technical Writer": "docs",
                    "Researcher": "researcher"}[r]
    return "unknown"


@pytest.fixture
def profile() -> Profile:
    return Profile(name="test", slots={
        "frontier": ["fake/big"], "coder": ["fake/small", "fake/small2"],
        "coder_escalation": ["fake/big"], "reasoner": ["fake/mid"], "cheap": ["fake/tiny"]},
        limits={"max_revision_rounds": 3, "escalate_after": 2, "max_usd_per_run": 0})


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp_path / "test_app.py").write_text("from app import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    (tmp_path / "README.md").write_text("# demo\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    return tmp_path


@pytest.fixture
def cfg() -> ProjectConfig:
    return ProjectConfig(profile="test", test_command="python -m pytest -q -p no:cacheprovider", sandbox="local")
