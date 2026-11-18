"""`codeguild bench` with scripted models: the plumbing (fresh projects, merging, hidden checks,
the report). What it is for, measuring real models, happens on the owner's machine and in the
nightly job."""

from __future__ import annotations

import json
import sys

from typer.testing import CliRunner

from codeguild.bench import bench_once, write_report
from codeguild.cli import app

from .conftest import FakeProvider

APPROVE = {"verdict": "approve", "findings": [], "summary": "fine"}
ACCEPT = {"decision": "ACCEPT", "notes": "good", "improvements": []}
DONE = {"status": "done", "summary": "implemented", "files_changed": ["app.py", "test_app.py"]}
DOCS_DONE = {"files_changed": [], "summary": "nothing to document"}

GOOD_APP = """def add(a, b):
    return a + b


def subtract(a, b):
    return a - b


def multiply(a, b):
    return a * b


def divide(a, b):
    if b == 0:
        raise ValueError("division by zero")
    return a / b
"""
GOOD_TESTS = """import pytest

from app import add, divide, multiply, subtract


def test_add():
    assert add(2, 3) == 5


def test_all():
    assert subtract(5, 3) == 2 and multiply(4, 3) == 12
    assert divide(9, 3) == 3
    with pytest.raises(ValueError):
        divide(1, 0)
"""


WEAK_TESTS = "from app import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"


def _engineer(app_text: str, n_tasks: int = 4) -> list:
    turns = []
    for _ in range(n_tasks):
        turns += [
            [("read_file", {"path": "app.py"}), ("read_file", {"path": "test_app.py"})],
            [
                ("write_file", {"path": "app.py", "content": app_text}),
                ("write_file", {"path": "test_app.py", "content": GOOD_TESTS}),
            ],
            DONE,
        ]
    return turns


def _team(app_text: str = GOOD_APP) -> FakeProvider:
    plan = {
        "roadmap": ["calculator"],
        "tasks": [
            {
                "id": f"T{i}",
                "title": title,
                "description": f"add {title} to app.py with tests, as described in the goal",
                "files": ["app.py", "test_app.py"],
                "done_when": "pytest passes",
                "depends_on": [f"T{i - 1}"] if i > 1 else [],
            }
            for i, title in enumerate(["subtract", "multiply", "divide", "tests"], 1)
        ],
        "risks": [],
        "questions_for_owner": [],
    }
    return FakeProvider(
        {
            "lead": [plan] + [ACCEPT] * 4,
            "engineer": _engineer(app_text),
            "critic": [APPROVE] * 4,
            "security": [APPROVE] * 4,
            "docs": [DOCS_DONE] * 4,
        }
    )


def _bench(tmp_path, profile, use_models, provider, **kw):
    use_models(provider)
    return bench_once(
        1,
        tmp_path / "run-1",
        profile,
        fixed_plan=kw.pop("fixed_plan", False),
        test_command=f'"{sys.executable}" -m pytest -q -p no:cacheprovider',
        timeout_s=kw.pop("timeout_s", 300),
        report=lambda level, msg: None,
    )


def test_a_run_where_everything_works_passes(tmp_path, profile, use_models):
    r = _bench(tmp_path, profile, use_models, _team())
    assert r.error == "" and r.passed, r
    assert [t.id for t in r.tasks] == ["T1", "T2", "T3", "T4"]
    assert all(t.accepted and t.merged for t in r.tasks)
    assert r.tests_pass_after_merge and all(r.acceptance.values()) and len(r.acceptance) == 5
    assert r.calls > 0 and r.input_tokens > 0
    # the hidden checks were never part of the project the agents worked in
    assert not (tmp_path / "run-1" / "acceptance.py").exists()


def test_the_hidden_checks_catch_what_the_team_missed(tmp_path, profile, use_models):
    wrong = GOOD_APP.replace('        raise ValueError("division by zero")\n', "        return 0\n")
    # its own test expects ValueError, so make the visible tests agree with the wrong code
    team = _team(wrong)
    team.turns["engineer"] = [
        t
        if not isinstance(t, list) or t[0][0] != "write_file"
        else [
            ("write_file", {"path": "app.py", "content": wrong}),
            ("write_file", {"path": "test_app.py", "content": WEAK_TESTS}),
        ]
        for t in team.turns["engineer"]
    ]
    r = _bench(tmp_path, profile, use_models, team)
    assert all(t.accepted and t.merged for t in r.tasks) and r.tests_pass_after_merge
    assert r.acceptance["divide by zero"] is False and not r.passed


def test_a_rejected_task_stops_the_run_and_says_why(tmp_path, profile, use_models):
    team = _team()
    reject = {
        "verdict": "request_changes",
        "summary": "subtract is backwards",
        "findings": [{"severity": "high", "file": "app.py", "issue": "b - a"}],
    }
    team.turns["critic"] = [reject] * 10
    team.turns["lead"] = team.turns["lead"][:1] + [{"decision": "REJECT", "notes": "no"}] * 10
    r = _bench(tmp_path, profile, use_models, team)
    assert not r.passed and not r.tasks[0].accepted
    assert "subtract is backwards" in r.tasks[0].why_not or r.tasks[0].why_not
    assert [t.why_not for t in r.tasks[1:]] == ["not reached"] * 3


def test_the_fixed_plan_skips_the_lead_planning(tmp_path, profile, use_models):
    team = _team()
    team.turns["lead"] = [ACCEPT] * 4  # no plan reply: the lead must not be asked to plan
    r = _bench(tmp_path, profile, use_models, team, fixed_plan=True)
    assert r.passed and [t.title for t in r.tasks][:2] == ["subtract", "multiply"]


def test_report_files(tmp_path, profile, use_models):
    r = _bench(tmp_path, profile, use_models, _team())
    md = write_report([r], tmp_path, profile)
    assert md.read_text().startswith("# codeguild bench: 1/1 runs passed")
    assert json.loads((tmp_path / "report.json").read_text())[0]["passed"] is True


def test_the_command(tmp_path, profile, use_models, project):
    use_models(_team())
    (project / ".codeguild" / "profiles").mkdir(parents=True, exist_ok=True)
    (project / ".codeguild" / "profiles" / "test.yaml").write_text(json.dumps(profile.model_dump()))
    out = tmp_path / "bench"
    res = CliRunner().invoke(
        app, ["bench", "-n", "1", "-C", str(project), "-p", "test", "--dir", str(out)]
    )
    assert res.exit_code == 0, res.output
    assert "1/1 runs passed" in res.output and (out / "report.md").exists()
    assert (out / "run-1" / "app.py").exists()
    again = CliRunner().invoke(app, ["bench", "-C", str(project), "-p", "test", "--dir", str(out)])
    assert again.exit_code == 2 and "not empty" in again.output
