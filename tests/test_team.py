"""Your own team: which roles take part (per project) and which model plays each role (in any
profile, including your own profiles kept for all projects)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from codeguild import team
from codeguild.cli import app
from codeguild.config import (
    ConfigError,
    Profile,
    ProjectConfig,
    list_available,
    load_profile,
    load_project_config,
    load_role,
    save_profile,
    where_defined,
)
from codeguild.plan import Plan, Task
from codeguild.trace import read_trace
from codeguild.workflow import Guild

from .conftest import FakeProvider
from .test_pair import make_ui  # noqa: F401 — fixture

APPROVE = {"verdict": "approve", "findings": [], "summary": "fine"}
WRITE = [("write_file", {"path": "b.py", "content": "y = 2\n"})]
DONE = {"status": "done", "summary": "wrote b.py"}


# ---------------------------------------------------------------------------- the profile
def test_a_role_with_its_own_models_uses_them(project, cfg, profile):
    profile = profile.model_copy(update={"roles": {"critic": ["fake/critic-model"]}})
    cfg.roles_enabled = ["engineer", "critic"]
    g = Guild(project, cfg, profile)
    g.router.register_provider(
        "fake", FakeProvider({"engineer": [WRITE, DONE], "critic": [APPROVE]})
    )
    plan = Plan(goal="g", roadmap=[], tasks=[Task(id="T1", title="t", description="d" * 50)])
    assert g.run_tasks(plan, ["T1"])[0].accepted
    g.close()
    used = {e["role"]: e["model"] for e in read_trace(g.trace.path) if e["kind"] == "model_call"}
    assert used == {"critic": "fake/critic-model", "engineer": "fake/small"}


def test_role_models_are_shown_and_counted_like_slots(profile):
    profile = profile.model_copy(update={"roles": {"lead": ["ollama/qwen3:32b"]}})
    assert profile.models_for(load_role("lead")) == ["ollama/qwen3:32b"]
    assert profile.models_for(load_role("docs")) == ["fake/tiny"]  # its slot, cheap
    assert profile.all_chains()["role:lead"] == ["ollama/qwen3:32b"]


@pytest.mark.parametrize(
    "roles",
    [{"engineer": []}, {"engineer": ["qwen2.5-coder"]}, {"engineer": ["ollama/has space"]}],
)
def test_role_models_must_be_model_ids(profile, roles):
    with pytest.raises(ValueError):
        Profile.model_validate({**profile.model_dump(), "roles": roles})


def test_every_built_in_profile_still_loads_and_has_no_role_models():
    for name in ("free", "lite", "pro"):
        assert load_profile(name).roles == {}


# ---------------------------------------------------------------------------- where profiles live
def test_your_profiles_are_found_in_every_project_and_a_project_copy_wins(tmp_path, profile):
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    mine = profile.model_copy(update={"name": "mine"})
    save_profile(mine, "user")
    assert "mine" in list_available("profiles", one) and "mine" in list_available("profiles", two)
    assert where_defined("profiles", "mine", two) == "user"
    save_profile(mine.model_copy(update={"description": "local"}), "project", one)
    assert where_defined("profiles", "mine", one) == "project"
    assert load_profile("mine", one).description == "local"
    assert where_defined("profiles", "free", one) == "built-in"


@pytest.mark.parametrize("name", ["Mine", "a b", "../x", "", "x" * 41])
def test_profile_names_are_checked(profile, name):
    with pytest.raises(ConfigError):
        save_profile(profile.model_copy(update={"name": name}), "user")


def test_editing_a_built_in_profile_copies_it_into_the_project(project, profile):
    free = load_profile("free", project)
    edited = team.set_role_models(free, "engineer", ["ollama/qwen2.5-coder:14b"], project)
    path, scope = team.save_edited(edited, project)
    assert scope == "project" and path == project / ".codeguild" / "profiles" / "free.yaml"
    assert load_profile("free", project).roles == {"engineer": ["ollama/qwen2.5-coder:14b"]}
    assert load_profile("free").roles == {}  # codeguild's own file is untouched


# ---------------------------------------------------------------------------- the team
def test_team_rules(project):
    cfg = ProjectConfig()
    assert team.set_team(cfg, project, ["docs", "ux", "engineer", "lead"]).roles_enabled == [
        "lead",
        "engineer",
        "ux",
        "docs",
    ]
    with pytest.raises(ConfigError, match="needs engineer"):
        team.set_team(cfg, project, ["lead", "critic"])
    with pytest.raises(ConfigError, match="no role named"):
        team.set_team(cfg, project, ["engineer", "designer"])
    with pytest.raises(ConfigError, match="not part of the task team"):
        team.set_team(cfg, project, ["engineer", "researcher"])


def test_role_model_rules(project, profile):
    with pytest.raises(ConfigError, match="unknown provider"):
        team.set_role_models(profile, "critic", ["nowhere/model"], project)
    with pytest.raises(ConfigError, match="does not use a model"):
        team.set_role_models(profile, "verifier", ["ollama/x"], project)
    with pytest.raises(ConfigError, match="no role named"):
        team.set_role_models(profile, "designer", ["ollama/x"], project)
    set_ = team.set_role_models(profile, "critic", [" ollama/a ", "", "anthropic/b"], project)
    assert set_.roles == {"critic": ["ollama/a", "anthropic/b"]}
    assert team.set_role_models(set_, "critic", [], project).roles == {}


def test_the_team_view(project, profile):
    cfg = ProjectConfig(roles_enabled=["lead", "engineer", "verifier", "critic"])
    profile = profile.model_copy(update={"roles": {"critic": ["ollama/x"]}})
    rows = {r.name: r for r in team.roles(project, cfg, profile)}
    assert rows["engineer"].required and rows["engineer"].on_team
    assert rows["security"].on_team is False and rows["researcher"].on_team is None
    assert rows["critic"].own_models and rows["critic"].models == ["ollama/x"]
    assert rows["verifier"].models == [] and not rows["verifier"].uses_models
    assert "security and privacy" in rows["security"].part
    names = [r.name for r in team.roles(project, cfg, profile)]
    assert names[:4] == ["lead", "engineer", "verifier", "critic"]  # the team first, in order


# ---------------------------------------------------------------------------- command line
@pytest.fixture
def initialised(project) -> Path:
    from codeguild.config import save_project_config

    save_project_config(project, ProjectConfig(profile="free"))
    return project


def _cli(*args):
    return CliRunner().invoke(app, [*args], catch_exceptions=False)


def test_team_commands(initialised):
    C = ["-C", str(initialised)]
    assert "writes the code" in _cli("team", *C).output.replace("\n", " ")
    assert _cli("team", "add", "ux", "performance", *C).exit_code == 0
    assert load_project_config(initialised).roles_enabled[-3:] == ["ux", "performance", "docs"]
    assert _cli("team", "remove", "docs", *C).exit_code == 0
    assert "docs" not in load_project_config(initialised).roles_enabled
    bad = _cli("team", "remove", "engineer", *C)
    assert bad.exit_code == 2 and "needs engineer" in bad.output


def test_profile_commands(initialised):
    C = ["-C", str(initialised)]
    made = _cli("profile", "new", "mine", "--from", "lite", "--all-projects", *C)
    assert made.exit_code == 0 and load_project_config(initialised).profile == "mine"
    assert (
        _cli("profile", "set", "engineer", "ollama/qwen2.5-coder:14b", "ollama/x:7b", *C).exit_code
        == 0
    )
    mine = load_profile("mine", initialised)
    assert mine.roles == {"engineer": ["ollama/qwen2.5-coder:14b", "ollama/x:7b"]}
    assert where_defined("profiles", "mine", initialised) == "user"
    assert "role engineer" in _cli("profile", "show", *C).output
    assert "your profiles" in _cli("profile", "list", *C).output
    assert _cli("profile", "unset", "engineer", *C).exit_code == 0
    assert load_profile("mine", initialised).roles == {}
    assert _cli("profile", "new", "mine", *C).exit_code == 2  # exists
    assert _cli("profile", "use", "free", *C).exit_code == 0
    assert load_project_config(initialised).profile == "free"
    assert _cli("profile", "set", "critic", "nowhere/x", *C).exit_code == 2


def test_python_dash_m_runs_the_new_commands(initialised):
    import subprocess
    import sys

    out = subprocess.run(
        [sys.executable, "-m", "codeguild", "team", "-C", str(initialised)],
        capture_output=True,
        text=True,
        timeout=60,
        env={**__import__("os").environ, "COLUMNS": "200"},
    )
    assert out.returncode == 0 and "engineer" in out.stdout


# ---------------------------------------------------------------------------- dashboard
def test_the_team_screen_api(make_ui, project):  # noqa: F811
    ui = make_ui(FakeProvider({}))
    view = ui.get("/api/team").json()
    assert view["profile"] == "test" and view["where"] == "project"
    assert {"name": "free", "where": "built-in"} in view["profiles"]
    assert any(r["name"] == "engineer" and r["required"] for r in view["roles"])
    assert "anthropic/claude-sonnet-5-5" in view["suggestions"]

    changed = ui.post(
        "/api/team",
        json={"roles_enabled": ["engineer", "critic", "ux"], "models": {"ux": ["ollama/x"]}},
    )
    assert changed.status_code == 200, changed.text
    assert load_project_config(project).roles_enabled == ["engineer", "critic", "ux"]
    assert load_profile("test", project).roles == {"ux": ["ollama/x"]}

    new = ui.post(
        "/api/team",
        json={"models": {"lead": ["ollama/big"]}, "save_as": "fast", "scope": "user"},
    )
    assert new.json()["profile"] == "fast" and new.json()["where"] == "user"
    assert load_project_config(project).profile == "fast"
    assert load_profile("fast", project).roles == {"ux": ["ollama/x"], "lead": ["ollama/big"]}

    assert ui.post("/api/team", json={"roles_enabled": ["critic"]}).status_code == 400
    assert ui.post("/api/team", json={"models": {"ux": ["bad"]}}).status_code == 400
    assert ui.post("/api/team", json={"save_as": "fast"}).status_code == 400  # exists
    # nothing was half-written by the refused calls
    assert load_project_config(project).roles_enabled == ["engineer", "critic", "ux"]


def test_the_team_screen_is_on_the_page(make_ui):  # noqa: F811
    page = make_ui(FakeProvider({})).get("/").text
    assert 'id="btnTeam"' in page and "function openTeam" in page
