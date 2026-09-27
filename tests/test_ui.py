from __future__ import annotations

import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from guild.config import save_project_config  # noqa: E402
from guild.ui import server  # noqa: E402
from tests.conftest import FakeProvider  # noqa: E402
from tests.test_workflow import APPROVE, DOCS, ENGINEER_GOOD, LEAD_ACCEPT, PLAN, VERIFIER_RUNS  # noqa: E402


@pytest.fixture
def client(project, cfg, profile, monkeypatch):
    save_project_config(project, cfg)
    # make the "test" profile resolvable from the project and inject the fake provider
    import yaml
    (project / ".guild" / "profiles").mkdir(parents=True, exist_ok=True)
    (project / ".guild" / "profiles" / "test.yaml").write_text(yaml.safe_dump(profile.model_dump()))
    fake = FakeProvider({"lead": [PLAN, LEAD_ACCEPT], "engineer": ENGINEER_GOOD, "verifier": VERIFIER_RUNS,
                         "critic": [APPROVE], "security": [APPROVE], "docs": DOCS})
    from guild.providers import router as router_mod
    orig = router_mod.Router._provider_for

    def patched(self, prefix):
        return fake if prefix == "fake" else orig(self, prefix)
    monkeypatch.setattr(router_mod.Router, "_provider_for", patched)
    app = server.create_app(project)
    return TestClient(app)


def _wait(client, kind, timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = client.get("/api/state")
        assert r.status_code == 200, r.text[:300]
        s = r.json()
        if not s["busy"] and s["job"] and s["job"]["kind"] == kind:
            return s
        time.sleep(0.05)
    raise AssertionError(f"job {kind} did not finish")


def test_index_and_state(client):
    assert "guild" in client.get("/").text
    s = client.get("/api/state").json()
    assert s["config"]["profile"] == "test" and s["plan"] is None and not s["busy"]
    assert "lead" in s["roles"] and "test" in s["profiles"]


def test_doctor_lists_slots(client):
    d = client.get("/api/doctor").json()
    assert {r["slot"] for r in d["rows"]} >= {"frontier", "coder"}
    assert all(r["status"] == "unknown provider" for r in d["rows"])  # fake/* isn't a real provider


def test_plan_then_run_via_api(client):
    assert client.post("/api/plan", json={"goal": "add multiply"}).json()["ok"]
    s = _wait(client, "plan")
    assert s["job"]["status"] == "done", s["job"]
    assert s["plan"]["tasks"][0]["id"] == "T1"

    assert client.post("/api/run", json={}).json()["ok"]
    s = _wait(client, "run")
    assert s["job"]["status"] == "done", s["job"]
    assert s["plan"]["tasks"][0]["status"] == "done"
    runs = client.get("/api/runs").json()
    assert len(runs) == 2 and runs[0]["calls"] > 0
    run_with_engineer = next(r for r in runs if "implement" in r["phases"])
    detail = client.get(f"/api/runs/{run_with_engineer['id']}").json()
    ev = detail["events"]
    assert ev[0]["kind"] == "run_start" and ev[-1]["kind"] == "run_end"
    assert "engineer" in detail["by_role"] and detail["by_role"]["engineer"]["calls"] > 0


def test_conflict_when_busy(project):
    import threading
    hub = server.Hub(project)
    gate = threading.Event()
    hub.start("plan", lambda: gate.wait(5))
    with pytest.raises(Exception) as ei:
        hub.start("plan", lambda: None)
    assert getattr(ei.value, "status_code", None) == 409
    gate.set()
    time.sleep(0.1)
    assert hub.job.status == "done"


def test_task_edit_and_delete(client):
    client.post("/api/plan", json={"goal": "add multiply"})
    _wait(client, "plan")
    p = client.post("/api/plan/task", json={"id": "T1", "title": "renamed", "status": "done"}).json()
    assert p["tasks"][0]["title"] == "renamed" and p["tasks"][0]["status"] == "done"
    p = client.post("/api/plan/task", json={"id": "T1", "delete": True}).json()
    assert p["tasks"] == []


def test_hub_replays_history_to_late_subscribers(project):
    hub = server.Hub(project)
    hub.publish({"kind": "phase", "phase": "plan"})
    hub.publish({"kind": "model_call", "role": "lead"})
    q = hub.subscribe()
    got = [q.get(timeout=1) for _ in range(2)]
    assert [e["kind"] for e in got] == ["phase", "model_call"]
    hub.publish({"kind": "job_done"})
    assert q.get(timeout=1)["kind"] == "job_done"
    hub.unsubscribe(q)
    hub.publish({"kind": "x"})
    assert q.empty()


def test_files_and_file_read(client, project):
    d = client.get("/api/files").json()
    paths = {f["path"] for f in d["files"]}
    assert {"app.py", "test_app.py", "README.md"} <= paths and not any(p.startswith(".guild") for p in paths)
    f = client.get("/api/file", params={"path": "app.py"}).json()
    assert "def add" in f["content"] and not f["binary"]
    assert client.get("/api/file", params={"path": "../../etc/passwd"}).status_code == 403
    assert client.get("/api/file", params={"path": "nope.py"}).status_code == 404


def test_project_switch_and_fs(client, project, tmp_path_factory):
    other = tmp_path_factory.mktemp("other")
    (other / "main.py").write_text("print(1)\n")
    info = client.get("/api/project").json()
    assert info["root"] == str(project.resolve())
    fs = client.get("/api/fs", params={"path": str(other.parent)}).json()
    assert any(d["path"] == str(other) for d in fs["dirs"])
    r = client.post("/api/project", json={"path": str(other)}).json()
    assert r["root"] == str(other.resolve()) and r["initialised"]  # default config written
    assert {f["path"] for f in client.get("/api/files").json()["files"]} == {"main.py"}
    assert client.post("/api/project", json={"path": str(other / "missing")}).status_code == 400


def test_chat_roundtrip(client, monkeypatch):
    from guild.providers import router as router_mod
    from guild.providers.base import Completion, Message, Usage

    class Talker:
        def complete(self, model_name, messages, tools, max_tokens, temperature, on_token=None):
            assert "Assistant" in messages[0].content
            prior_users = [m.content for m in messages if m.role == "user"]
            return Completion(Message("assistant", f"answer to: {prior_users[-1]} (seen {len(prior_users)} user msgs)"),
                              Usage(1, 1), f"fake/{model_name}", "stop")
    orig = router_mod.Router._provider_for
    monkeypatch.setattr(router_mod.Router, "_provider_for", lambda self, p: Talker() if p == "fake" else orig(self, p))
    client.post("/api/chat", json={"message": "what does app.py do?"})
    _wait(client, "chat")
    hist = client.get("/api/chat").json()
    assert hist[0]["role"] == "user" and hist[1]["role"] == "assistant" and "answer to: what does app.py do?" in hist[1]["content"]
    client.post("/api/chat", json={"message": "and the tests?"})
    _wait(client, "chat")
    hist = client.get("/api/chat").json()
    assert len(hist) == 4 and "seen 3 user msgs" in hist[3]["content"]  # context msg + 2 turns
    client.delete("/api/chat")
    assert client.get("/api/chat").json() == []


def test_revise_plan_keeps_done_tasks(client, monkeypatch):
    from guild.providers import router as router_mod
    from guild.providers.base import Completion, Message, Usage
    import json as _json
    revised = {"roadmap": ["m1"], "tasks": [
        {"id": "T1", "title": "add multiply", "description": "add multiply(a,b) to app.py with a test", "files": ["app.py"], "done_when": "test_multiply passes", "depends_on": []},
        {"id": "T2", "title": "add divide", "description": "add divide(a,b) with ZeroDivisionError handling and tests", "files": ["app.py"], "done_when": "test_divide passes", "depends_on": ["T1"]}],
        "risks": [], "questions_for_owner": []}
    from tests.test_workflow import PLAN

    class Lead:
        def __init__(self):
            self.n = 0
        def complete(self, model_name, messages, tools, max_tokens, temperature, on_token=None):
            self.n += 1
            return Completion(Message("assistant", _json.dumps(PLAN if self.n == 1 else revised)), Usage(1, 1), f"fake/{model_name}", "stop")
    orig = router_mod.Router._provider_for
    lead = Lead()
    monkeypatch.setattr(router_mod.Router, "_provider_for", lambda self, p: lead if p == "fake" else orig(self, p))
    client.post("/api/plan", json={"goal": "arith"})
    _wait(client, "plan")
    client.post("/api/plan/task", json={"id": "T1", "status": "done"})
    client.post("/api/plan/revise", json={"instruction": "add a divide task after T1"})
    s = _wait(client, "revise")
    assert s["job"]["status"] == "done", s["job"]
    tasks = s["plan"]["tasks"]
    assert [t["id"] for t in tasks] == ["T1", "T2"] and tasks[0]["status"] == "done" and tasks[1]["status"] == "todo"


def test_state_reports_root(client, project):
    assert client.get("/api/state").json()["root"] == str(project.resolve())
