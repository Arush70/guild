"""The small modules the workflow, CLI and dashboard are built on, each through its own
interface: plan, repo, verify, paths, projectdocs, provider status, workspace."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from guild import projectdocs
from guild.config import Profile
from guild.paths import ProjectPaths, atomic_write_text
from guild.plan import Plan, PlanError, PlanStore, Task, UnknownTask
from guild.providers.status import is_pulled, profile_status
from guild.repo import Repo, TaskBranch, files_in_diff, slug
from guild.sandbox import LocalSandbox
from guild.tools import Workspace
from guild.verify import Baseline, Verifier


def _plan(*tasks: Task) -> Plan:
    return Plan(goal="g", roadmap=[], tasks=list(tasks))


def _task(tid: str, **kw) -> Task:
    return Task(id=tid, title=f"title {tid}", description="d", **kw)


# ---------------------------------------------------------------------------- plan
def test_next_task_waits_for_dependencies_and_resumes_interrupted_work():
    plan = _plan(_task("T1", status="in_progress"), _task("T2", depends_on=["T1"]))
    assert plan.next_task().id == "T1"  # left over from a killed run: runnable again
    assert plan.next_task(skip={"T1"}) is None  # T2 still waits for T1
    plan.find("T1").status = "done"
    assert plan.next_task().id == "T2"


def test_a_dependency_that_is_not_in_the_plan_does_not_block():
    plan = _plan(_task("T1", depends_on=["GONE", "T1"]))
    assert plan.unmet_dependencies(plan.find("T1")) == []
    assert plan.next_task().id == "T1"


def test_require_names_the_missing_task_and_the_known_ones():
    plan = _plan(_task("T1"))
    assert plan.require("T1")[0].id == "T1"
    with pytest.raises(UnknownTask, match=r"no task T9 .*tasks: T1"):
        plan.require("T1", "T9")


def test_edit_task_rejects_bad_values_and_changes_nothing():
    plan = _plan(_task("T1"))
    with pytest.raises(PlanError):
        plan.edit_task("T1", {"status": "finished"})
    with pytest.raises(PlanError):
        plan.edit_task("T1", {"title": 5})
    assert plan.find("T1").status == "todo" and plan.find("T1").title == "title T1"
    assert plan.edit_task("T1", {"status": "blocked"}).status == "blocked"


def test_a_revised_plan_keeps_what_was_done_under_the_same_id():
    old = _plan(
        _task("T1", status="done", branch="guild/t1-x", merged=True, notes="n"),
        _task("T2", status="blocked", branch="guild/t2-y"),
    )
    new = Plan.from_lead(
        "g2",
        {"tasks": [{"id": "T1", "title": "a"}, {"id": "T2", "title": "b"}, {"title": "c"}]},
        previous=old,
    )
    t1, t2, t3 = new.tasks
    assert (t1.status, t1.merged, t1.branch, t1.notes) == ("done", True, "guild/t1-x", "n")
    assert (t2.status, t2.branch) == ("todo", "guild/t2-y")  # not done: gets another go
    assert (t3.id, t3.status) == ("T3", "todo")
    assert new.created == old.created


def test_plan_store_round_trip_and_unreadable_file(tmp_path: Path):
    store = PlanStore(tmp_path / ".guild" / "plan.json")
    assert store.load() is None
    store.save(_plan(_task("T1", files=["a.py"])))
    assert store.load().find("T1").files == ["a.py"]
    store.path.write_text("{not json")
    with pytest.raises(PlanError, match="not a readable plan"):
        store.load()


# ---------------------------------------------------------------------------- repo
def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def repo(project: Path) -> Repo:
    return Repo(project)


def test_slug_is_always_a_valid_branch_component():
    assert slug("Task 1: set~up  the ^thing", 40) == "task-1-set-up-the-thing"
    assert slug("---", 10) == ""
    assert not slug("a" * 9 + "-b", 10).endswith("-")


def test_files_in_diff_lists_each_file_once():
    diff = "diff --git a/x.py b/x.py\n+1\ndiff --git a/d/y.py b/d/y.py\ndiff --git a/x.py b/x.py\n"
    assert files_in_diff(diff) == ["d/y.py", "x.py"]


def test_diff_is_none_on_a_git_error_not_an_error_message(repo: Repo):
    assert repo.diff("no-such-ref") is None
    assert repo.diff("HEAD") == ""


def test_commit_all_leaves_guild_files_and_the_owners_untracked_files(repo: Repo):
    root = repo.root
    (root / "mine.txt").write_text("owner's scratch file\n")
    before = repo.untracked()
    (root / "new.py").write_text("x = 1\n")
    (root / ".guild").mkdir(exist_ok=True)
    (root / ".guild" / "plan.json").write_text("{}")
    assert repo.commit_all("add new", leave_untracked=before).committed
    committed = _git(root, "show", "--name-only", "--format=", "HEAD").split()
    assert committed == ["new.py"]
    assert "mine.txt" in repo.untracked()
    assert not repo.commit_all("nothing to do", leave_untracked=before).committed


def test_merge_refuses_a_dirty_tree_and_discard_refuses_foreign_branches(repo: Repo):
    root = repo.root
    base = repo.current_branch()
    _git(root, "checkout", "-q", "-b", "guild/t1-x")
    (root / "new.py").write_text("x = 1\n")
    repo.commit_all("task")
    _git(root, "checkout", "-q", base)

    (root / "app.py").write_text("changed = True\n")
    assert "dirty" in repo.merge_branch("guild/t1-x", base)["error"]
    _git(root, "checkout", "--", "app.py")

    assert repo.discard_branch(base)["ok"] is False and repo.branch_exists(base)
    merged = repo.merge_branch("guild/t1-x", base)
    assert merged["ok"] and merged["base"] == base
    assert (root / "new.py").exists() and not repo.branch_exists("guild/t1-x")


def test_task_branch_commits_the_work_and_returns_to_where_it_started(repo: Repo):
    warnings: list[str] = []
    start = repo.current_branch()
    branch = TaskBranch.start(repo, "T1", "Add a thing", dry_run=False, warn=warnings.append)
    assert branch.name == "guild/t1-add-a-thing" == repo.current_branch()
    (repo.root / "thing.py").write_text("x = 1\n")
    assert "thing.py" in branch.review_diff()
    branch.commit("guild: T1")
    branch.leave()
    assert repo.current_branch() == start and not (repo.root / "thing.py").exists()
    assert "thing.py" in repo.branch_diff(branch.name, start)["diff"]
    assert warnings == []


def test_task_branch_in_a_dry_run_or_without_git_has_no_name(repo: Repo, tmp_path: Path):
    assert TaskBranch.start(repo, "T1", "x", dry_run=True, warn=print).name is None
    bare = tmp_path / "nogit"
    bare.mkdir()
    assert TaskBranch.start(Repo(bare), "T1", "x", dry_run=False, warn=print).name is None


# ---------------------------------------------------------------------------- verify
def test_verifier_reports_failures_and_missing_tests(project: Path):
    verifier = Verifier(LocalSandbox(project), "python -m pytest -q -p no:cacheprovider")
    baseline = verifier.baseline()
    assert baseline.tests == {"test_app.py::test_add"}
    ok = verifier.run(baseline)
    assert ok.passed and ok.tests_run == 1 and ok.removed_tests == []

    (project / "test_app.py").write_text("def test_other():\n    assert 1 == 2\n")
    bad = verifier.run(baseline)
    assert not bad.passed and bad.exit_code != 0
    assert bad.removed_tests == ["test_app.py::test_add"]
    assert any("EXISTING TESTS REMOVED" in f for f in bad.failures)
    assert any("test_other" in f for f in bad.failures)


def test_lint_runs_only_after_passing_tests_and_can_fail_the_run(project: Path):
    sandbox = LocalSandbox(project)
    v = Verifier(sandbox, "python -m pytest -q -p no:cacheprovider", 'python -c "exit(3)"').run()
    assert not v.passed and v.lint_passed is False and "lint:" in v.failures[-1]
    v = Verifier(sandbox, 'python -c "exit(1)"', 'python -c "exit(3)"').run()
    assert not v.passed and v.lint_passed is None


def test_a_non_pytest_runner_is_compared_by_test_count(project: Path):
    verifier = Verifier(LocalSandbox(project), "python -c \"print('2 passed')\"")
    assert verifier.collect_tests() is None
    assert verifier.run(Baseline(None, 2)).passed
    dropped = verifier.run(Baseline(None, 5))
    assert not dropped.passed and "dropped from 5 to 2" in dropped.failures[-1]


# ---------------------------------------------------------------------------- paths, docs
def test_project_paths_and_newest_trace_first(tmp_path: Path):
    paths = ProjectPaths(tmp_path)
    assert not paths.initialised and not paths.has_git and paths.traces() == []
    atomic_write_text(paths.config, "profile: free\n")
    assert paths.initialised and paths.config.parent == paths.guild_dir
    for run_id in ("20260101-000000-aaaaaa", "20260102-000000-bbbbbb"):
        atomic_write_text(paths.trace(run_id), "{}\n")
    import os

    os.utime(paths.trace("20260101-000000-aaaaaa"), (1, 1))
    assert [p.parent.name for p in paths.traces()] == [
        "20260102-000000-bbbbbb",
        "20260101-000000-aaaaaa",
    ]


def test_atomic_write_replaces_the_file_and_leaves_no_temp_file(tmp_path: Path):
    target = tmp_path / "deep" / "f.json"
    atomic_write_text(target, "one")
    atomic_write_text(target, "two")
    assert target.read_text() == "two"
    assert [p.name for p in target.parent.iterdir()] == ["f.json"]


def test_project_docs_scaffold_once_and_read_first_location(tmp_path: Path):
    created = projectdocs.scaffold(tmp_path)
    assert "docs/PRD.md" in created and "RULES.md" in created
    assert not (tmp_path / "docs" / "DESIGN.md").exists()  # read if present, never scaffolded
    assert projectdocs.scaffold(tmp_path) == []

    (tmp_path / "RULES.md").write_text("rule " * 2000)
    (tmp_path / "CLAUDE.md").write_text("ignored: RULES.md comes first")
    (tmp_path / "TASKS.md").write_text("   \n")
    docs = projectdocs.read(tmp_path, per_doc_chars=100)
    assert docs["Rules (RULES.md)"].endswith("…(truncated)")
    assert not any(k.startswith("Tasks") for k in docs)  # empty documents are left out


# ---------------------------------------------------------------------------- provider status
def test_profile_status_levels(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    profile = Profile(
        name="p",
        slots={
            "local": ["ollama/qwen2.5-coder", "ollama/llama3:8b"],
            "cloud": ["anthropic/some-model", "nowhere/x"],
        },
    )
    rows = {r.model: r for r in profile_status(profile, pulled=["qwen2.5-coder:7b"])}
    assert rows["ollama/qwen2.5-coder"].ok
    assert rows["ollama/llama3:8b"].level == "missing"
    assert "ollama pull llama3:8b" in rows["ollama/llama3:8b"].status
    assert rows["anthropic/some-model"].level == "missing"
    assert rows["nowhere/x"].level == "broken"
    assert all(r.level == "broken" for r in profile_status(profile, None) if r.slot == "local")
    assert is_pulled("llama3", ["llama3:8b"]) and not is_pulled("llama3", ["llama2:7b"])


# ---------------------------------------------------------------------------- workspace
def test_workspace_scopes_paths_and_skips_ignored_files(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "big.js").write_text("x")
    (tmp_path / "notes.log").write_text("x")
    ws = Workspace(tmp_path, ignore=["node_modules", "*.log"])

    with pytest.raises(PermissionError, match="escapes project root"):
        ws.resolve("../outside.txt")
    assert ws.rel_parts("src/../src/a.py") == ("src", "a.py")
    assert [rel for rel, _ in ws.walk(tmp_path)] == [str(Path("src") / "a.py")]
    assert "big.js" not in ws.listing() and "a.py" in ws.listing()
    assert ws.listing("src/a.py").startswith("not a directory")
