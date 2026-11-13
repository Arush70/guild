"""Behaviour the 2026-11-12 restructuring changed by accident and an independent review caught.
Each test is one of those differences, pinned to what codeguild did before."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from codeguild.cli import app
from codeguild.config import ProjectConfig, load_role, save_project_config
from codeguild.plan import Plan, Task
from codeguild.trace import jsonable
from codeguild.workflow import Guild, TaskOutcome

from .conftest import FakeProvider
from .test_pair import NEW_FILE, _idle, _pair_script, _until, make_ui  # noqa: F401 — fixture

APPROVE = {"verdict": "approve", "findings": [], "summary": "fine"}
REJECT = {
    "verdict": "request_changes",
    "summary": "CRITIC-SAYS-NO",
    "findings": [{"severity": "high", "file": "b.py", "issue": "FINDING-TEXT"}],
}
WRITE = [("write_file", {"path": "b.py", "content": "y = 2\n"})]
DONE = {"status": "done", "summary": "wrote b.py"}


@pytest.fixture
def small_team() -> ProjectConfig:
    return ProjectConfig(
        profile="test", test_command='python -c "pass"', roles_enabled=["engineer", "critic"]
    )


def _guild(project, cfg, profile, turns) -> tuple[Guild, FakeProvider]:
    g = Guild(project, cfg, profile)
    fake = FakeProvider(turns)
    g.router.register_provider("fake", fake)
    return g, fake


def _one_task() -> Plan:
    return Plan(goal="g", roadmap=[], tasks=[Task(id="T1", title="one", description="d" * 50)])


def test_engineer_gets_the_push_back_after_two_replies_without_changes(
    project, small_team, profile
):
    turns = {"engineer": [DONE, DONE, WRITE, DONE], "critic": [APPROVE]}
    g, fake = _guild(project, small_team, profile, turns)
    plan = _one_task()
    out = g.run_tasks(plan, ["T1"])
    g.close()
    assert out[0].accepted and plan.find("T1").status == "done"
    assert sum(1 for role, _ in fake.calls if role == "engineer") == 4


def test_a_projects_own_engineer_role_keeps_the_profile_tool_budget(project, small_team, profile):
    packaged = load_role("engineer", project).model_dump()
    packaged.pop("task_budget")  # a role file written before the field existed
    (project / ".codeguild" / "roles").mkdir(parents=True)
    (project / ".codeguild" / "roles" / "engineer.yaml").write_text(yaml.safe_dump(packaged))
    profile.limits.max_tool_calls_per_task = 150
    g = Guild(project, small_team, profile)
    assert g.agent("engineer").max_tool_calls == 150
    assert g.agent("critic").max_tool_calls != 150
    g.close()


def test_a_task_is_never_based_on_a_guild_branch(project, small_team, profile):
    """Started from a codeguild branch no task owns, then re-run from its own branch: both times
    the merge target is the default branch."""
    subprocess.run(["git", "checkout", "-q", "-b", "codeguild/t0-old"], cwd=project, check=True)
    plan = _one_task()
    for content in ("y = 2\n", "y = 33\n"):
        write = [
            ("list_files", {}),
            ("write_file", {"path": f"b{len(content)}.py", "content": content}),
        ]
        turns = {"engineer": [write, DONE], "critic": [APPROVE]}
        g, _ = _guild(project, small_team, profile, turns)
        plan.find("T1").status = "todo"
        assert g.run_tasks(plan, ["T1"])[0].accepted
        g.close()
        assert plan.find("T1").base is None
    task = plan.find("T1")
    merged = g.repo.merge_branch(task.branch, task.base)
    assert merged["ok"] and merged["base"] == g.repo.default_branch()


def test_job_results_keep_their_shape_and_leave_the_message_history_out(
    project, small_team, profile
):
    g, _ = _guild(project, small_team, profile, {"lead": ["An answer."]})
    answer = jsonable({"result": g.ask("lead", "why?")})["result"]
    g.close()
    assert "history" not in answer and {"raw", "model", "role"} <= set(answer)

    task = _one_task().tasks[0]
    out = TaskOutcome(task, True, 1, None, {"critic": APPROVE}, None, None, False, 0.0)
    sent = jsonable(out)
    assert sent["critic"] == APPROVE and sent["security"] is None and "reviews" not in sent
    assert sent["task"]["id"] == "T1"


@pytest.fixture
def cli_project(project, small_team, profile, use_models):
    save_project_config(project, small_team)
    (project / ".codeguild" / "profiles").mkdir(parents=True, exist_ok=True)
    profile.limits.max_revision_rounds = 1
    (project / ".codeguild" / "profiles" / "test.yaml").write_text(
        yaml.safe_dump(profile.model_dump())
    )
    plan = Plan(
        goal="g",
        roadmap=[],
        tasks=[Task(id=i, title=i, description="d" * 50, done_when="tests pass") for i in ("T1",)],
    )
    Guild(project, small_team, profile).plans.save(plan)
    return project


def test_guild_run_shows_why_a_task_was_not_accepted(cli_project: Path, use_models):
    use_models(FakeProvider({"engineer": [WRITE, DONE], "critic": [REJECT]}))
    res = CliRunner().invoke(app, ["run", "T1", "-C", str(cli_project)])
    assert "NOT ACCEPTED" in res.output and "FINDING-TEXT" in res.output
    assert "stopping: task not accepted" in res.output


def test_declining_a_task_does_not_claim_there_is_nothing_to_run(cli_project: Path, use_models):
    use_models(FakeProvider({}))
    res = CliRunner().invoke(app, ["run", "-C", str(cli_project)], input="n\n")
    assert res.exit_code == 0 and "no runnable tasks left" not in res.output


def test_remember_on_a_command_approval_does_not_cover_later_edits(make_ui, project):  # noqa: F811
    command = ("run_command", {"command": "pip --version"})
    ui = make_ui(_pair_script(command, NEW_FILE))
    ui.post("/api/pair", json={"message": "check pip, then make hello"})
    pending = lambda: ui.get("/api/state").json()["pending_approvals"]  # noqa: E731
    first = _until(pending, "command approval")[0]
    assert first["kind"] == "command"
    ui.post(f"/api/approve/{first['id']}", json={"approve": True, "remember": True})
    second = _until(lambda: [r for r in pending() if r["id"] != first["id"]], "edit approval")[0]
    assert second["kind"] == "edit" and not (project / "hello.py").exists()
    ui.post(f"/api/approve/{second['id']}", json={"approve": False})
    _idle(ui, "pair")
