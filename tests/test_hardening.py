"""Regression tests from the 2026-11-12 break-it audit.

Each test reproduces a defect found by attacking guild the way an untrusted model, a careless
small model, or an interrupted run would. They all failed before the fixes in that audit.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from guild.agent import Agent, extract_text_tool_calls
from guild.config import ProjectConfig, Role
from guild.providers.base import Completion, Message, ToolCall, Usage
from guild.providers.router import Router
from guild.sandbox import LocalSandbox
from guild.tools.registry import ToolContext, _readonly_allowed, dispatch
from guild.trace import Trace, read_trace
from guild.workflow import Guild, Plan, Task
from tests.conftest import FakeProvider
from tests.test_workflow import APPROVE, ENG_TOUCH, PLAN


def _ctx(root, **kw) -> ToolContext:
    return ToolContext(
        root=root,
        ignore=ProjectConfig().ignore,
        test_command="python -m pytest -q",
        lint_command=None,
        sandbox=LocalSandbox(root),
        **kw,
    )


def _guild(project, cfg, profile, fake):
    g = Guild(project, cfg, profile)
    g.router.register_provider("fake", fake)
    return g


# ------------------------------------------------------------------ read-only command allowlist


@pytest.mark.parametrize(
    "cmd",
    [
        "ls & touch pwned",  # single & is a separator in sh and cmd.exe
        "ls -la\ntouch pwned",  # newline is a separator
        "ls -la\r\ntouch pwned",
        "find . -delete",
        "find . -name '*.py' -exec rm {} +",
        "find . -fprint pwned",
        "git branch -D main",
        "git branch -m main other",
        "git branch newbranch",
        "sort -o app.py app.py",
        "sort --output=app.py app.py",
        "uniq app.py pwned",
        "tree -o pwned",
        "git diff --output=pwned",
        "git log --output=pwned",
        "git grep -O sh x",
        "rg --pre ./evil x",
        "ruff check --add-noqa",
        "pytest --basetemp=.",
        "pytest -p evil",
        "git diff --ext-diff",
    ],
)
def test_readonly_allowlist_refuses_bypasses(cmd):
    assert not _readonly_allowed(cmd), cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "pytest -q",
        "python -m pytest -q tests/test_app.py",
        "pytest -q | tail -20",
        "git log --oneline -5",
        "git branch",
        "git branch -a",
        "git branch --show-current",
        "find . -name '*.py'",
        "sort app.py",
        "uniq -c app.py",
        "git diff HEAD~1",
        "ls -la",
    ],
)
def test_readonly_allowlist_still_allows_inspection(cmd):
    assert _readonly_allowed(cmd), cmd


def test_readonly_run_command_does_not_execute_chained_command(project):
    ctx = _ctx(project, readonly=True)
    for cmd in ("ls & touch pwned", "ls -la\ntouch pwned", "find . -name pwned -o -delete"):
        out = dispatch(ctx, "run_command", {"command": cmd})
        assert out.startswith("refused"), (cmd, out)
    assert not (project / "pwned").exists()
    assert (project / "app.py").exists()


# ------------------------------------------------------------------ tool argument injection


def test_git_diff_ref_cannot_inject_options(project):
    ctx = _ctx(project, readonly=True)
    (project / "app.py").write_text("def add(a, b):\n    return a - b\n")
    target = project.parent / "pwned.txt"
    out = dispatch(ctx, "git_diff", {"ref": f"--output={target}"})
    assert out.startswith("refused"), out
    assert not target.exists()
    assert "return a - b" in dispatch(ctx, "git_diff", {"ref": "HEAD"})


# ------------------------------------------------------------------ protected directories


@pytest.mark.parametrize(
    "path",
    [
        ".git/hooks/pre-commit",
        ".git/config",
        ".guild/config.yaml",
        ".guild/plan.json",
        ".guild/roles/critic.yaml",
        "sub/../.git/hooks/pre-commit",
        ".GIT/hooks/pre-commit",
    ],
)
def test_write_tools_refuse_git_and_guild_dirs(project, path):
    ctx = _ctx(project)
    out = dispatch(ctx, "write_file", {"path": path, "content": "#!/bin/sh\necho pwned\n"})
    assert out.startswith("refused"), out
    assert not (project / ".git" / "hooks" / "pre-commit").exists()
    assert not (project / ".guild" / "plan.json").exists()


def test_edit_file_refuses_guild_config(project):
    (project / ".guild").mkdir()
    (project / ".guild" / "config.yaml").write_text("sandbox: docker\n")
    ctx = _ctx(project)
    out = dispatch(
        ctx,
        "edit_file",
        {
            "path": ".guild/config.yaml",
            "old_text": "sandbox: docker",
            "new_text": "sandbox: local",
        },
    )
    assert out.startswith("refused"), out
    assert "docker" in (project / ".guild" / "config.yaml").read_text()


def test_docs_allowlist_cannot_write_markdown_inside_git(project):
    ctx = _ctx(project, write_allowlist=["*.md", "docs/**", "**/*.md"])
    out = dispatch(ctx, "write_file", {"path": ".git/x.md", "content": "x"})
    assert out.startswith("refused"), out


def test_search_does_not_read_through_symlink_out_of_project(project, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "secret.txt"
    outside.write_text("API_KEY=sk-super-secret\n")
    try:
        os.symlink(outside, project / "link.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    ctx = _ctx(project)
    assert "sk-super-secret" not in dispatch(ctx, "search", {"pattern": "API_KEY"})
    assert dispatch(ctx, "read_file", {"path": "link.txt"}).startswith("refused")


# ------------------------------------------------------------------ small-model tolerance


def test_tools_accept_stringly_typed_and_extra_arguments(project):
    ctx = _ctx(project)
    out = dispatch(ctx, "list_files", {"path": ".", "max": "2"})
    assert "truncated at 2" in out, out
    out = dispatch(ctx, "read_file", {"path": "app.py", "start_line": "1", "end_line": "1"})
    assert "def add" in out and "return" not in out, out
    # 7b models add keys like "explanation"; the call must still run
    out = dispatch(ctx, "read_file", {"path": "app.py", "explanation": "looking at the code"})
    assert "def add" in out, out


def test_search_accepts_a_file_path(project):
    ctx = _ctx(project)
    assert "app.py:1:" in dispatch(ctx, "search", {"pattern": "def add", "path": "app.py"})


def test_text_tool_call_with_unbalanced_brace_in_content():
    """A write_file whose content has a lone brace inside a string used to be dropped."""
    code = 'def f():\n    return "}"\n'
    txt = "I'll write it.\n" + json.dumps(
        {"name": "write_file", "arguments": {"path": "a.py", "content": code}}
    )
    calls = extract_text_tool_calls(txt, {"write_file"})
    assert len(calls) == 1 and calls[0].arguments["content"] == code
    code2 = 'OPEN = "{"\n'
    txt2 = json.dumps({"name": "write_file", "arguments": {"path": "b.py", "content": code2}})
    txt2 += "\n" + json.dumps({"name": "run_tests", "arguments": {}})
    calls = extract_text_tool_calls(txt2, {"write_file", "run_tests"})
    assert [c.name for c in calls] == ["write_file", "run_tests"]


def test_text_tool_calls_inside_think_block_are_not_executed():
    txt = (
        '<think>maybe I should call {"name": "write_file", "arguments": {"path": "x", '
        '"content": ""}} ... no, read first</think>\n'
        '{"name": "read_file", "arguments": {"path": "app.py"}}'
    )
    calls = extract_text_tool_calls(txt, {"write_file", "read_file"})
    assert [c.name for c in calls] == ["read_file"]


def test_edit_file_preserves_crlf_line_endings(project):
    p = project / "win.py"
    p.write_bytes(b"def a():\r\n    return 1\r\n\r\ndef b():\r\n    return 2\r\n")
    ctx = _ctx(project)
    out = dispatch(
        ctx,
        "edit_file",
        {
            "path": "win.py",
            "old_text": "def a():\n    return 1\n",
            "new_text": "def a():\n    return 10\n",
        },
    )
    assert out == "edited win.py", out
    raw = p.read_bytes()
    assert raw == b"def a():\r\n    return 10\r\n\r\ndef b():\r\n    return 2\r\n"


def test_edit_file_preserves_lf_line_endings(project):
    p = project / "unix.py"
    p.write_bytes(b"a = 1\nb = 2\n")
    ctx = _ctx(project)
    dispatch(ctx, "edit_file", {"path": "unix.py", "old_text": "a = 1", "new_text": "a = 3"})
    assert p.read_bytes() == b"a = 3\nb = 2\n"


def test_overwrite_keeps_existing_line_endings(project):
    p = project / "win.txt"
    p.write_bytes(b"one\r\ntwo\r\n")
    ctx = _ctx(project)
    dispatch(ctx, "read_file", {"path": "win.txt"})
    dispatch(ctx, "write_file", {"path": "win.txt", "content": "one\ntwo\nthree\n"})
    assert p.read_bytes() == b"one\r\ntwo\r\nthree\r\n"


# ------------------------------------------------------------------ agent loop


class _Scripted:
    """Provider that replays completions and records the message list of every call."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.seen: list[list[Message]] = []

    def complete(self, model_name, messages, tools, max_tokens, temperature):
        self.seen.append(list(messages))
        turn = self.turns.pop(0) if self.turns else '{"status": "done"}'
        if isinstance(turn, list):
            calls = [
                ToolCall(id=f"c{len(self.seen)}_{i}", name=n, arguments=a)
                for i, (n, a) in enumerate(turn)
            ]
            msg = Message("assistant", "", tool_calls=calls)
        else:
            msg = Message("assistant", turn)
        return Completion(msg, Usage(1, 1), f"fake/{model_name}", "stop")


def _agent(project, profile, role: Role, provider, tmp_path) -> Agent:
    router = Router(profile)
    router.register_provider("fake", provider)
    trace = Trace(tmp_path / "t.jsonl", "r1")
    return Agent(role, router, _ctx(project, readonly=bool(role.readonly)), trace)


def test_tool_results_directly_follow_the_tool_calls_when_budget_runs_out(
    project, profile, tmp_path
):
    """OpenAI and Anthropic reject a request where a user message sits between an assistant
    tool call and its tool result."""
    role = Role(
        name="x",
        title="X",
        slot="coder",
        tools=["read_file"],
        max_tool_calls=1,
        system_prompt="s",
    )
    prov = _Scripted(
        [
            [("read_file", {"path": "app.py"})],
            [("read_file", {"path": "test_app.py"})],  # over budget
            '{"ok": true}',
        ]
    )
    res = _agent(project, profile, role, prov, tmp_path).run("go")
    assert res.data == {"ok": True}
    for msgs in prov.seen:
        for i, m in enumerate(msgs):
            if m.role == "assistant" and m.tool_calls:
                following = msgs[i + 1 : i + 1 + len(m.tool_calls)]
                assert [f.role for f in following] == ["tool"] * len(m.tool_calls), [
                    x.role for x in msgs
                ]


def test_agent_refuses_tools_the_role_does_not_have(project, profile, tmp_path):
    """A model can emit a call to any registered tool; only the role's own tools may run."""
    role = Role(name="docsish", title="D", slot="coder", tools=["read_file"], system_prompt="s")
    prov = _Scripted(
        [
            [("run_command", {"command": "touch pwned"})],
            [("write_file", {"path": "pwned2", "content": "x"})],
            '{"ok": true}',
        ]
    )
    res = _agent(project, profile, role, prov, tmp_path).run("go")
    assert res.ok
    assert not (project / "pwned").exists() and not (project / "pwned2").exists()
    tool_msgs = [m for m in prov.seen[-1] if m.role == "tool"]
    assert all("not available to this role" in m.content for m in tool_msgs), tool_msgs


def test_profile_tool_budget_applies_to_the_engineer(project, cfg, profile):
    profile.limits.max_tool_calls_per_task = 7
    g = Guild(project, cfg, profile)
    try:
        assert g.agent("engineer").max_tool_calls == 7
        assert g.agent("critic").max_tool_calls == g.role("critic").max_tool_calls
    finally:
        g.close()


# ------------------------------------------------------------------ router / spend


class _Counting:
    def __init__(self):
        self.calls: list[str] = []

    def complete(self, model_name, messages, tools, max_tokens, temperature):
        self.calls.append(model_name)
        return Completion(Message("assistant", "{}"), Usage(1000, 1000), f"fake/{model_name}", "")


def test_free_profile_never_calls_a_priced_model(profile):
    """The $0 cap used to be checked after the request was made (and paid for)."""
    router = Router(profile)  # max_usd_per_run == 0
    router.tracker.prices["fake/small"] = (3.0, 15.0)
    prov = _Counting()
    router.register_provider("fake", prov)
    comp = router.complete("coder", [Message("user", "hi")])
    assert comp.model == "fake/small2"
    assert prov.calls == ["small2"]
    assert router.tracker.total_usd == 0


def test_prices_yaml_overrides_default_prices(project, cfg, profile):
    (project / ".guild").mkdir(exist_ok=True)
    (project / ".guild" / "prices.yaml").write_text(
        "fake/small: [1.0, 2.0]\nfake/big: 0\nbroken: nonsense\n"
    )
    g = Guild(project, cfg, profile)
    try:
        assert g.tracker.prices["fake/small"] == (1.0, 2.0)
        assert g.tracker.prices["fake/big"] == (0.0, 0.0)
        assert "broken" not in g.tracker.prices
    finally:
        g.close()


# ------------------------------------------------------------------ trace


def test_read_trace_skips_a_half_written_last_line(tmp_path):
    p = tmp_path / "trace.jsonl"
    p.write_text('{"seq": 1, "kind": "run_start"}\n{"seq": 2, "kind": "mod', encoding="utf-8")
    assert [e["seq"] for e in read_trace(p)] == [1]


def test_trace_survives_unserialisable_values_and_none_previews(tmp_path):
    t = Trace(tmp_path / "t.jsonl", "r")
    t.emit("note", weird=object(), raw=b"bytes")
    t.model_call(
        role="r",
        slot="s",
        model="m",
        input_tokens=1,
        output_tokens=1,
        usd=0.0,
        latency_s=0.0,
        n_tool_calls=0,
        stop_reason="",
        content_preview=None,  # type: ignore[arg-type]
    )
    t.close()
    t.emit("note", msg="after close must not raise")
    assert len(read_trace(t.path)) == 2


# ------------------------------------------------------------------ plan / schema


def test_plan_schema_dedupes_ids_and_coerces_numeric_depends_on():
    from guild.schemas import validate

    data, err = validate(
        "lead",
        "plan",
        {
            "tasks": [
                {"id": "T1", "title": "a"},
                {"id": "T1", "title": "b", "depends_on": [1]},
                {"id": 3, "title": "c", "depends_on": ["T1", 2]},
            ]
        },
    )
    assert err is None, err
    ids = [t["id"] for t in data["tasks"]]
    assert len(set(ids)) == 3, ids


def test_lead_review_accepts_common_synonyms():
    from guild.schemas import validate

    for word, want in (
        ("accepted", "ACCEPT"),
        ("Approve", "ACCEPT"),
        ("accept.", "ACCEPT"),
        ("revision", "REVISE"),
        ("request_changes", "REVISE"),
    ):
        data, err = validate("lead", "review", {"decision": word})
        assert err is None and data["decision"] == want, (word, err)


def test_dangling_dependency_does_not_crash_or_deadlock(project, cfg, profile):
    """Deleting a task in the dashboard left its id in other tasks' depends_on: run_tasks
    raised KeyError and next_task() never returned the dependant."""
    plan = Plan(
        goal="g",
        roadmap=[],
        tasks=[Task(id="T2", title="t", description="d" * 50, depends_on=["T1"])],
    )
    assert plan.next_task() is plan.tasks[0]
    fake = FakeProvider({"engineer": ENG_TOUCH, "critic": [APPROVE], "security": [APPROVE]})
    (project / "notes.txt").write_text("x\n")
    g = _guild(project, cfg, profile, fake)
    try:
        outs = g.run_tasks(plan, ["T2"], roles=["engineer", "critic", "security"])
        assert len(outs) == 1
        with pytest.raises(RuntimeError, match="no task"):
            g.run_tasks(plan, ["NOPE"])
    finally:
        g.close()


def test_stale_in_progress_task_is_runnable_again():
    plan = Plan(
        goal="g",
        roadmap=[],
        tasks=[Task(id="T1", title="t", description="d", status="in_progress")],
    )
    assert plan.next_task() is plan.tasks[0]


def test_revise_plan_keeps_merged_flag(project, cfg, profile):
    fake = FakeProvider({"lead": [PLAN]})
    g = _guild(project, cfg, profile, fake)
    try:
        done = Task(**PLAN["tasks"][0])
        done.status, done.merged, done.branch = "done", True, "guild/t1-x"
        g.save_plan(Plan(goal="g", roadmap=[], tasks=[done]))
        new = g.revise_plan("add a second task")
        assert new.tasks[0].status == "done" and new.tasks[0].merged is True
    finally:
        g.close()


# ------------------------------------------------------------------ workflow / git


def _branch(project) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=project, capture_output=True, text=True
    ).stdout.strip()


def _committed_files(project, ref="HEAD") -> set[str]:
    out = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", ref], cwd=project, capture_output=True, text=True
    ).stdout
    return set(out.split())


def test_task_id_with_spaces_and_symbols_still_gets_a_branch(project, cfg, profile):
    fake = FakeProvider({"engineer": ENG_TOUCH, "critic": [APPROVE], "security": [APPROVE]})
    (project / "notes.txt").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-qm", "notes"], cwd=project, check=True)
    g = _guild(project, cfg, profile, fake)
    plan = Plan(
        goal="g",
        roadmap=[],
        tasks=[Task(id="Task 1: set~up^..", title="???", description="d" * 50)],
    )
    try:
        out = g.run_task(plan, plan.tasks[0], roles=["engineer", "critic", "security"])
    finally:
        g.close()
    assert out.accepted
    assert out.task.branch and out.task.branch.startswith("guild/task-1-set-up")
    assert _branch(project) == out.task.branch


def test_work_done_after_blocked_pushback_is_committed_and_reviewed(project, cfg, profile):
    """First reply 'blocked' → push-back → engineer creates a new file. That file was never
    committed (and so missing from the reviewers' diff and from the branch)."""
    fake = FakeProvider(
        {
            "engineer": [
                {"status": "blocked", "summary": "calc.py does not exist"},
                [
                    (
                        "write_file",
                        {"path": "calc.py", "content": "def sub(a, b):\n    return a - b\n"},
                    )
                ],
                {"status": "done", "summary": "created", "files_changed": ["calc.py"]},
            ],
            "critic": [APPROVE],
            "security": [APPROVE],
        }
    )
    g = _guild(project, cfg, profile, fake)
    plan = Plan(goal="g", roadmap=[], tasks=[Task(id="T1", title="calc", description="d" * 50)])
    try:
        out = g.run_task(plan, plan.tasks[0], roles=["engineer", "critic", "security"])
        crit_ctx = next(e for e in read_trace(g.trace.path) if e["kind"] == "review_input")
    finally:
        g.close()
    assert out.accepted
    assert "calc.py" in _committed_files(project)
    assert "calc.py" in crit_ctx["files_in_diff"]
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=project, capture_output=True, text=True
    ).stdout
    assert "calc.py" not in status


def test_lead_revise_notes_reach_the_task(project, cfg, profile):
    fake = FakeProvider(
        {
            "engineer": ENG_TOUCH,
            "critic": [APPROVE],
            "security": [APPROVE],
            "lead": [
                {"decision": "REVISE", "notes": "rename the helper", "improvements": ["add types"]}
            ],
        }
    )
    (project / "notes.txt").write_text("x\n")
    g = _guild(project, cfg, profile, fake)
    plan = Plan(goal="g", roadmap=[], tasks=[Task(id="T1", title="t", description="d" * 50)])
    try:
        out = g.run_task(plan, plan.tasks[0], roles=["engineer", "critic", "security", "lead"])
    finally:
        g.close()
    assert not out.accepted and out.task.status == "todo"
    assert "rename the helper" in out.task.notes and "add types" in out.task.notes


def test_interrupt_does_not_leave_task_stuck_in_progress(project, cfg, profile):
    class Boom:
        def complete(self, *a, **k):
            raise KeyboardInterrupt

    g = Guild(project, cfg, profile)
    g.router.register_provider("fake", Boom())
    plan = Plan(goal="g", roadmap=[], tasks=[Task(id="T1", title="t", description="d" * 50)])
    with pytest.raises(KeyboardInterrupt):
        g.run_task(plan, plan.tasks[0], roles=["engineer"])
    g.close()
    assert plan.tasks[0].status == "todo"
    assert g.load_plan().tasks[0].status == "todo"
    assert _branch(project) in ("main", "master")


def test_commit_works_without_a_git_identity(project, cfg, profile, monkeypatch, tmp_path):
    """Fresh git installs have no user.name/email; `git commit` failed silently and the task
    was 'accepted' with nothing on its branch."""
    (project / "notes.txt").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-qm", "notes"], cwd=project, check=True)
    # Hiding the global/system config below must not also change line-ending handling: on
    # Windows core.autocrlf=true comes from there, and without it every CRLF file suddenly
    # looks modified (a dirty tree, so guild rightly makes no branch). Pin the current value.
    autocrlf = subprocess.run(
        ["git", "config", "--get", "core.autocrlf"], cwd=project, capture_output=True, text=True
    ).stdout.strip()
    subprocess.run(["git", "config", "core.autocrlf", autocrlf or "false"], cwd=project, check=True)
    subprocess.run(["git", "config", "--unset", "user.email"], cwd=project, check=True)
    subprocess.run(["git", "config", "--unset", "user.name"], cwd=project, check=True)
    empty = tmp_path / "emptyhome"
    empty.mkdir()
    for var in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(var, str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("EMAIL", "")
    fake = FakeProvider({"engineer": ENG_TOUCH, "critic": [APPROVE], "security": [APPROVE]})
    probe = subprocess.run(
        ["git", "commit", "--allow-empty", "-qm", "probe"], cwd=project, capture_output=True
    )
    if probe.returncode == 0:
        pytest.skip("git can derive an identity on this machine")
    g = _guild(project, cfg, profile, fake)
    plan = Plan(goal="g", roadmap=[], tasks=[Task(id="T1", title="t", description="d" * 50)])
    try:
        out = g.run_task(plan, plan.tasks[0], roles=["engineer", "critic", "security"])
    finally:
        g.close()
    assert out.accepted
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=project, capture_output=True, text=True
    ).stdout
    assert out.task.branch, f"no branch was created; git status:\n{status}"
    log = subprocess.run(
        ["git", "log", "--oneline", "-3"], cwd=project, capture_output=True, text=True
    ).stdout
    assert "guild(T1)" in log, log


def test_review_diff_is_not_a_git_error_message(project, cfg, profile):
    g = Guild(project, cfg, profile)
    try:
        text = g._diff_text("does-not-exist-ref")
    finally:
        g.close()
    assert "fatal" not in text and "unknown revision" not in text, text


def test_review_diff_includes_new_files_when_no_branch_was_created(project, cfg, profile):
    """Dirty tree → no branch, no commits → new files are untracked and were invisible to the
    reviewers (plain `git diff` does not show untracked files)."""
    (project / "app.py").write_text("def add(a, b):\n    return a + b  # edited\n")  # dirty
    fake = FakeProvider(
        {
            "engineer": [
                [("write_file", {"path": "brand_new.py", "content": "X = 1\n"})],
                {"status": "done", "summary": "s", "files_changed": ["brand_new.py"]},
            ],
            "critic": [APPROVE],
            "security": [APPROVE],
        }
    )
    g = _guild(project, cfg, profile, fake)
    plan = Plan(goal="g", roadmap=[], tasks=[Task(id="T1", title="t", description="d" * 50)])
    try:
        out = g.run_task(plan, plan.tasks[0], roles=["engineer", "critic", "security"])
        ev = next(e for e in read_trace(g.trace.path) if e["kind"] == "review_input")
    finally:
        g.close()
    assert out.task.branch is None
    assert "brand_new.py" in ev["files_in_diff"]


def test_discard_refuses_non_guild_branches(project, cfg, profile):
    subprocess.run(["git", "branch", "precious"], cwd=project, check=True)
    g = Guild(project, cfg, profile)
    try:
        res = g.discard_branch("precious")
    finally:
        g.close()
    assert res["ok"] is False
    branches = subprocess.run(["git", "branch"], cwd=project, capture_output=True, text=True).stdout
    assert "precious" in branches


def test_merge_goes_back_to_the_branch_the_task_started_from(project, cfg, profile):
    """A task started on `develop` was merged into main/master by the dashboard."""
    start = _branch(project)
    subprocess.run(["git", "checkout", "-q", "-b", "develop"], cwd=project, check=True)
    (project / "notes.txt").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-qm", "develop work"], cwd=project, check=True)
    fake = FakeProvider({"engineer": ENG_TOUCH, "critic": [APPROVE], "security": [APPROVE]})
    g = _guild(project, cfg, profile, fake)
    plan = Plan(goal="g", roadmap=[], tasks=[Task(id="T1", title="t", description="d" * 50)])
    try:
        out = g.run_task(plan, plan.tasks[0], roles=["engineer", "critic", "security"])
        assert out.accepted and out.task.base == "develop"
        res = g.merge_branch(out.task.branch, out.task.base)
    finally:
        g.close()
    assert res["ok"] and res["base"] == "develop", res
    assert "notes.txt" not in _committed_files(project, start)


def test_config_rejects_unknown_sandbox():
    with pytest.raises(ValueError):
        ProjectConfig(sandbox="dockr")
    assert ProjectConfig(sandbox="Docker").sandbox == "docker"
