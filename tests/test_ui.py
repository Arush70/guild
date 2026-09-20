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
    detail = client.get(f"/api/runs/{runs[0]['id']}").json()
    assert detail[0]["kind"] == "run_start" and detail[-1]["kind"] == "run_end"


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
