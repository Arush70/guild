"""Pair programming (interactive mode): approval gate, modes, cancel, server and CLI."""

from __future__ import annotations

import json
import threading
import time

import pytest
from typer.testing import CliRunner

from codeguild.config import ProjectConfig, save_project_config
from codeguild.errors import Cancelled
from codeguild.plan import Plan, Task
from codeguild.providers.base import Completion, Message, Usage
from codeguild.sandbox import LocalSandbox
from codeguild.tools import ToolContext, dispatch
from codeguild.tools.registry import approval_request
from codeguild.trace import read_trace
from codeguild.workflow import Guild, PairSession
from tests.conftest import FakeProvider

NEW_FILE = ("write_file", {"path": "hello.py", "content": "print('hi')\n"})
EDIT_APP = (
    "edit_file",
    {"path": "app.py", "old_text": "return a + b", "new_text": "return b + a"},
)


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


# ------------------------------------------------------------------ the gate itself


def test_approval_request_describes_edits_and_commands(project):
    ctx = _ctx(project)
    req = approval_request(ctx, *NEW_FILE)
    assert req["kind"] == "edit" and req["new_file"] and req["path"] == "hello.py"
    assert "+print('hi')" in req["preview"] and req["proposed"] == "print('hi')\n"
    req = approval_request(ctx, *EDIT_APP)
    assert not req["new_file"] and "-    return a + b" in req["preview"]
    assert "+    return b + a" in req["preview"] and "def add" in req["proposed"]
    req = approval_request(ctx, "run_command", {"command": "pip install requests"})
    assert req == {
        "kind": "command",
        "tool": "run_command",
        "command": "pip install requests",
        "preview": "pip install requests",
    }


def test_nothing_to_approve_when_the_call_is_harmless_or_will_be_refused(project):
    ctx = _ctx(project)
    # inspection commands, no-op edits, invalid edits, protected paths, unread overwrites
    assert approval_request(ctx, "run_command", {"command": "git status"}) is None
    assert approval_request(ctx, "run_command", {"command": "pytest -q"}) is None
    assert (
        approval_request(ctx, "edit_file", {"path": "app.py", "old_text": "nope", "new_text": "x"})
        is None
    )
    assert (
        approval_request(ctx, "write_file", {"path": ".git/hooks/pre-commit", "content": "x"})
        is None
    )
    assert approval_request(ctx, "write_file", {"path": "../out.py", "content": "x"}) is None
    assert approval_request(ctx, "write_file", {"path": "app.py", "content": "x = 1\n"}) is None
    assert approval_request(ctx, "read_file", {"path": "app.py"}) is None


def test_dispatch_asks_first_and_a_decline_changes_nothing(project):
    asked: list[dict] = []
    answers = iter([(False, "use a different name"), (True, "")])

    def approver(req):
        asked.append(req)
        return next(answers)

    ctx = _ctx(project, approver=approver)
    out = dispatch(ctx, *NEW_FILE)
    assert out.startswith("refused: the owner declined") and "use a different name" in out
    assert not (project / "hello.py").exists()
    out = dispatch(ctx, *NEW_FILE)
    assert out.startswith("created hello.py") and (project / "hello.py").exists()
    assert [a["path"] for a in asked] == ["hello.py", "hello.py"]
    # reads and inspection commands never ask
    dispatch(ctx, "read_file", {"path": "app.py"})
    dispatch(ctx, "run_command", {"command": "git status"})
    assert len(asked) == 2


def test_command_is_not_run_when_declined(project):
    ctx = _ctx(project, approver=lambda req: (False, ""))
    out = dispatch(ctx, "run_command", {"command": "python -c \"open('pwned','w')\""})
    assert out.startswith("refused") and not (project / "pwned").exists()


# ------------------------------------------------------------------ Guild.pair


def _pair_script(*tool_turns, final="Done: created hello.py."):
    return FakeProvider({"pair": [*[[t] for t in tool_turns], final]})


def test_pair_ask_mode_applies_approved_changes_in_the_working_tree(project, cfg, profile):
    g = _guild(project, cfg, profile, _pair_script(NEW_FILE))
    asked = []
    try:
        res = g.pair(
            "make hello.py", session=PairSession(), ask=lambda r: (asked.append(r), (True, ""))[1]
        )
        events = read_trace(g.trace.path)
    finally:
        g.close()
    assert res.raw == "Done: created hello.py."
    assert res.data["files_changed"] == ["hello.py"] and len(asked) == 1
    assert (project / "hello.py").read_text() == "print('hi')\n"
    approvals = [e for e in events if e["kind"] == "approval"]
    assert approvals and approvals[0]["approved"] is True and approvals[0]["target"] == "hello.py"
    # no branch, no commit: the owner reviews live and commits themselves
    import subprocess

    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=project, capture_output=True, text=True
    ).stdout.strip()
    assert not branch.startswith("codeguild/")
    assert (
        "hello.py"
        in subprocess.run(
            ["git", "status", "--porcelain"], cwd=project, capture_output=True, text=True
        ).stdout
    )


def test_pair_without_an_approver_fails_closed(project, cfg, profile):
    g = _guild(project, cfg, profile, _pair_script(NEW_FILE, final="could not"))
    try:
        res = g.pair("make hello.py", session=PairSession())
    finally:
        g.close()
    assert not (project / "hello.py").exists() and res.data["files_changed"] == []


def test_pair_auto_edits_mode_only_asks_for_commands(project, cfg, profile):
    cmd = ("run_command", {"command": "python -c \"open('ran','w')\""})
    g = _guild(project, cfg, profile, _pair_script(NEW_FILE, cmd))
    asked = []
    try:
        g.pair(
            "go", session=PairSession("auto-edits"), ask=lambda r: (asked.append(r), (True, ""))[1]
        )
    finally:
        g.close()
    assert (project / "hello.py").exists() and (project / "ran").exists()
    assert [a["kind"] for a in asked] == ["command"]


def test_pair_plan_mode_is_read_only(project, cfg, profile):
    cmd = ("run_command", {"command": "python -c \"open('ran','w')\""})
    g = _guild(project, cfg, profile, _pair_script(NEW_FILE, cmd, final="Plan: 1. …"))
    try:
        res = g.pair("how would you do it?", session=PairSession("plan"), ask=lambda r: (True, ""))
        with pytest.raises(ValueError):
            PairSession("yolo")
    finally:
        g.close()
    assert not (project / "hello.py").exists() and not (project / "ran").exists()
    assert res.raw.startswith("Plan:")


def test_pair_keeps_the_safety_model(project, cfg, profile):
    """Approval is an extra gate, not a replacement: protected paths stay refused even if the
    owner would say yes, and they are never even asked."""
    (project / ".env").write_text("KEY=secret\n")
    turns = [
        ("write_file", {"path": ".git/hooks/pre-commit", "content": "#!/bin/sh\n"}),
        ("write_file", {"path": ".codeguild/config.yaml", "content": "sandbox: local\n"}),
        ("read_file", {"path": ".env"}),
    ]
    g = _guild(project, cfg, profile, _pair_script(*turns, final="nope"))
    asked = []
    try:
        res = g.pair(
            "do bad things", session=PairSession(), ask=lambda r: (asked.append(r), (True, ""))[1]
        )
    finally:
        g.close()
    assert asked == []
    assert not (project / ".git" / "hooks" / "pre-commit").exists()
    assert not any("KEY=secret" in m.content for m in res.history)


# ------------------------------------------------------------------ cancel


def test_cancel_stops_before_the_next_model_call(project, cfg, profile):
    g = Guild(project, cfg, profile)

    class CancelsItself:
        calls = 0

        def complete(self, model_name, messages, tools, max_tokens, temperature, on_token=None):
            self.calls += 1
            g.cancel()
            return Completion(
                Message("assistant", "", tool_calls=[]), Usage(1, 1), f"fake/{model_name}", "stop"
            )

    prov = CancelsItself()
    g.router.register_provider("fake", prov)
    plan = Plan(goal="g", roadmap=[], tasks=[Task(id="T1", title="t", description="d" * 50)])
    with pytest.raises(Cancelled):
        g.run_task(plan, plan.tasks[0], roles=["engineer"])
    g.close()
    assert prov.calls == 1
    assert plan.tasks[0].status == "todo" and "interrupted" in plan.tasks[0].notes


def test_cancel_kills_a_running_command(project):
    sb = LocalSandbox(project, threading.Event())
    threading.Timer(0.6, sb.cancel.set).start()
    t0 = time.time()
    with pytest.raises(Cancelled):
        sb.run('python -c "import time; time.sleep(60)"', timeout=120)
    assert time.time() - t0 < 20


def test_sandbox_timeout_still_works(project):
    res = LocalSandbox(project).run('python -c "import time; time.sleep(30)"', timeout=1)
    assert res.timed_out and res.returncode == 124


# ------------------------------------------------------------------ server

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from codeguild.ui import server  # noqa: E402


@pytest.fixture
def make_ui(project, cfg, profile, use_models):
    import yaml

    save_project_config(project, cfg)
    (project / ".codeguild" / "profiles").mkdir(parents=True, exist_ok=True)
    (project / ".codeguild" / "profiles" / "test.yaml").write_text(
        yaml.safe_dump(profile.model_dump())
    )

    def make(fake):
        use_models(fake)
        return TestClient(server.create_app(project), base_url="http://127.0.0.1:7331")

    return make


def _until(fn, what, timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = fn()
        if v:
            return v
        time.sleep(0.03)
    raise AssertionError(f"timed out: {what}")


def _idle(client, kind):
    return _until(
        lambda: (lambda s: s if not s["busy"] and s["job"] and s["job"]["kind"] == kind else None)(
            client.get("/api/state").json()
        ),
        f"{kind} job to finish",
    )


def test_pair_endpoint_waits_for_approval_then_applies(make_ui, project):
    ui = make_ui(_pair_script(NEW_FILE, ("read_file", {"path": "app.py"}), EDIT_APP))
    assert ui.post("/api/pair", json={"message": "x", "mode": "yolo"}).status_code == 400
    assert ui.post("/api/pair", json={"message": "  "}).status_code == 400
    assert ui.post("/api/pair", json={"message": "make hello and flip add"}).json() == {"ok": True}
    assert ui.post("/api/chat", json={"message": "hi"}).status_code == 409  # one job at a time

    first = _until(lambda: ui.get("/api/state").json()["pending_approvals"], "approval request")[0]
    assert (
        first["path"] == "hello.py"
        and first["kind"] == "edit"
        and "+print('hi')" in first["preview"]
    )
    assert "event" not in first and "decision" not in first
    assert not (project / "hello.py").exists()  # nothing happens before the answer
    assert ui.post("/api/approve/nope", json={"approve": True}).status_code == 404
    assert ui.post(f"/api/approve/{first['id']}", json={"approve": True}).json() == {"ok": True}
    assert ui.post(f"/api/approve/{first['id']}", json={"approve": True}).status_code == 404

    second = _until(
        lambda: [
            a for a in ui.get("/api/state").json()["pending_approvals"] if a["id"] != first["id"]
        ],
        "second approval",
    )[0]
    assert second["path"] == "app.py"
    ui.post(f"/api/approve/{second['id']}", json={"approve": False, "reason": "leave add alone"})

    s = _idle(ui, "pair")
    assert s["job"]["status"] == "done" and s["pending_approvals"] == []
    assert s["job"]["result"]["files_changed"] == ["hello.py"]
    assert (project / "hello.py").exists()
    assert "return a + b" in (project / "app.py").read_text()
    chat = ui.get("/api/chat").json()
    assert [m["role"] for m in chat] == ["user", "assistant"] and chat[1]["files"] == ["hello.py"]


def test_approve_with_remember_covers_the_rest_of_the_turn(make_ui, project):
    second = ("write_file", {"path": "two.py", "content": "x = 2\n"})
    ui = make_ui(_pair_script(NEW_FILE, second))
    ui.post("/api/pair", json={"message": "two files"})
    first = _until(lambda: ui.get("/api/state").json()["pending_approvals"], "approval")[0]
    ui.post(f"/api/approve/{first['id']}", json={"approve": True, "remember": True})
    _idle(ui, "pair")
    assert (project / "hello.py").exists() and (project / "two.py").exists()


def test_cancel_endpoint_ends_a_turn_that_waits_for_approval(make_ui, project):
    ui = make_ui(_pair_script(NEW_FILE))
    assert ui.post("/api/cancel").json()["ok"] is False  # nothing running
    ui.post("/api/pair", json={"message": "make hello"})
    _until(lambda: ui.get("/api/state").json()["pending_approvals"], "approval")
    assert ui.post("/api/cancel").json() == {"ok": True, "job": "pair"}
    s = _idle(ui, "pair")
    assert s["job"]["status"] == "cancelled" and s["pending_approvals"] == []
    assert not (project / "hello.py").exists()
    # and the server is usable again
    assert ui.post("/api/pair", json={"message": "again", "mode": "plan"}).status_code == 200
    _idle(ui, "pair")


def test_chat_history_survives_a_server_restart(make_ui, project):
    fake = _pair_script(final="Nothing to do.")
    ui = make_ui(fake)
    ui.post("/api/pair", json={"message": "hello there", "mode": "plan"})
    _idle(ui, "pair")
    assert (project / ".codeguild" / "chat.jsonl").exists()
    again = make_ui(fake)  # a new app = a restarted server
    chat = again.get("/api/chat").json()
    assert [m["content"] for m in chat] == ["hello there", "Nothing to do."]
    assert again.delete("/api/chat").json() == []
    assert not (project / ".codeguild" / "chat.jsonl").exists()
    # a corrupt line must not break loading
    (project / ".codeguild" / "chat.jsonl").write_text(
        '{"role": "user", "content": "ok"}\n{broken\n'
    )
    assert len(make_ui(fake).get("/api/chat").json()) == 1


# ------------------------------------------------------------------ CLI


def test_cli_pair_session(project, cfg, profile, use_models):
    import yaml

    from codeguild.cli import app

    save_project_config(project, cfg)
    (project / ".codeguild" / "profiles").mkdir(parents=True, exist_ok=True)
    (project / ".codeguild" / "profiles" / "test.yaml").write_text(
        yaml.safe_dump(profile.model_dump())
    )
    second = ("write_file", {"path": "two.py", "content": "x = 2\n"})
    use_models(
        FakeProvider({"pair": [[NEW_FILE], [second], "First turn done.", "Second turn done."]})
    )
    # turn 1: approve hello.py (y), decline two.py (n); then switch mode, ask again, exit
    stdin = "y\nn\n/mode plan\nwhat next?\n/exit\n"
    res = CliRunner().invoke(app, ["pair", "make two files", "-C", str(project)], input=stdin)
    assert res.exit_code == 0, res.output
    assert "create hello.py" in res.output and "+print('hi')" in res.output
    assert "First turn done." in res.output and "changed: hello.py" in res.output
    assert "mode: plan" in res.output and "Second turn done." in res.output
    assert (project / "hello.py").exists() and not (project / "two.py").exists()

    assert (
        CliRunner().invoke(app, ["pair", "x", "-C", str(project), "--mode", "yolo"]).exit_code == 2
    )


def test_state_json_is_serialisable_with_pending_approvals(make_ui):
    ui = make_ui(_pair_script(NEW_FILE))
    ui.post("/api/pair", json={"message": "make hello"})
    _until(lambda: ui.get("/api/state").json()["pending_approvals"], "approval")
    assert json.dumps(ui.get("/api/state").json())
    ui.post("/api/cancel")
    _idle(ui, "pair")
