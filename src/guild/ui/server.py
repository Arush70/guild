"""Local web dashboard: `guild ui`.

Single-process FastAPI app. Long jobs (plan / run / ask) execute on one background thread;
their trace events and progress messages are pushed to the browser over Server-Sent Events.
Binds to 127.0.0.1 only — this is a local tool, not a hosted service.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from ..config import (GUILD_DIR, ProjectConfig, list_available, load_profile, load_project_config,
                      load_role, save_project_config)
from ..providers import AllProvidersFailed, BudgetExceeded
from ..providers.openai_compat import ENDPOINTS
from ..trace import read_trace


# --------------------------------------------------------------------------- job runner

@dataclass
class Job:
    kind: str
    status: str = "running"  # running | done | error
    started: float = field(default_factory=time.time)
    result: Any = None
    error: str | None = None


class Hub:
    """One background worker + fan-out of events to any number of SSE subscribers."""

    def __init__(self, root: Path):
        self.root = root
        self.lock = threading.Lock()
        self.job: Job | None = None
        self.subscribers: list[queue.Queue] = []
        self.history: list[dict] = []  # last N events for late joiners

    def publish(self, ev: dict) -> None:
        ev.setdefault("ts", time.time())
        with self.lock:
            self.history.append(ev)
            self.history = self.history[-500:]
            subs = list(self.subscribers)
        for q in subs:
            q.put(ev)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self.lock:
            self.subscribers.append(q)
            for ev in self.history[-200:]:
                q.put(ev)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def busy(self) -> bool:
        return self.job is not None and self.job.status == "running"

    def start(self, kind: str, fn) -> Job:
        if self.busy():
            raise HTTPException(409, f"a {self.job.kind} job is already running")
        job = Job(kind=kind)
        self.job = job

        def _run():
            try:
                job.result = fn()
                job.status = "done"
                self.publish({"kind": "job_done", "job": kind, "result": _jsonable(job.result)})
            except (AllProvidersFailed, BudgetExceeded, RuntimeError, FileNotFoundError) as e:
                job.status = "error"
                job.error = str(e)
                self.publish({"kind": "job_error", "job": kind, "error": str(e)})
            except Exception as e:  # noqa: BLE001 — surface anything to the UI
                job.status = "error"
                job.error = f"{type(e).__name__}: {e}"
                self.publish({"kind": "job_error", "job": kind, "error": job.error})

        threading.Thread(target=_run, daemon=True).start()
        return job


def _jsonable(v: Any) -> Any:
    if hasattr(v, "to_dict"):
        return v.to_dict()
    if hasattr(v, "__dataclass_fields__"):
        return {k: _jsonable(getattr(v, k)) for k in v.__dataclass_fields__ if k != "history"}
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_jsonable(x) for x in v]
    if isinstance(v, Path):
        return str(v)
    return v


# --------------------------------------------------------------------------- app

class PlanReq(BaseModel):
    goal: str
    context: str = ""
    profile: str | None = None


class RunReq(BaseModel):
    task_id: str | None = None
    all: bool = False
    skip: list[str] = []
    profile: str | None = None


class AskReq(BaseModel):
    role: str
    question: str
    profile: str | None = None


class ConfigReq(BaseModel):
    profile: str | None = None
    test_command: str | None = None
    sandbox: str | None = None
    roles_enabled: list[str] | None = None


def create_app(root: Path) -> FastAPI:
    app = FastAPI(title="guild", docs_url=None, redoc_url=None)
    hub = Hub(root)

    def _make_guild(profile_override: str | None):
        from ..workflow import Guild
        cfg = load_project_config(root)
        prof = load_profile(profile_override or cfg.profile, root)
        g = Guild(root, cfg, prof, report=lambda lvl, msg: hub.publish({"kind": "report", "level": lvl, "msg": msg}))
        g.trace.echo = lambda ev: hub.publish(dict(ev))
        return g

    @app.get("/", response_class=HTMLResponse)
    def index():
        return resources.files("guild.ui").joinpath("index.html").read_text(encoding="utf-8")

    @app.get("/api/state")
    def state():
        cfg = load_project_config(root)
        plan = _read_plan(root)
        return {
            "root": str(root), "config": cfg.model_dump(),
            "profiles": list_available("profiles", root), "roles": list_available("roles", root),
            "plan": plan, "busy": hub.busy(),
            "job": _jsonable(hub.job) if hub.job else None,
        }

    @app.get("/api/doctor")
    def doctor(profile: str | None = None):
        cfg = load_project_config(root)
        prof = load_profile(profile or cfg.profile, root)
        ollama = _ollama_models()
        rows = []
        for slot, chain in prof.slots.items():
            for m in chain:
                prefix, _, name = m.partition("/")
                if prefix == "ollama":
                    if ollama is None:
                        st, ok = "ollama not reachable", False
                    elif name in ollama or name.split(":")[0] in {x.split(":")[0] for x in ollama}:
                        st, ok = "ready", True
                    else:
                        st, ok = f"not pulled — ollama pull {name}", False
                elif prefix == "anthropic":
                    ok = bool(os.environ.get("ANTHROPIC_API_KEY"))
                    st = "key set" if ok else "ANTHROPIC_API_KEY missing"
                elif prefix in ENDPOINTS:
                    ep = ENDPOINTS[prefix]
                    ok = ep.key_env is None or bool(os.environ.get(ep.key_env)) or prefix in {"omniroute", "litellm", "custom"}
                    st = "configured" if ok else f"{ep.key_env} missing"
                else:
                    st, ok = "unknown provider", False
                rows.append({"slot": slot, "model": m, "status": st, "ok": ok})
        return {"profile": prof.model_dump(), "rows": rows, "git": (root / ".git").is_dir(),
                "ollama_models": ollama or []}

    @app.get("/api/roles")
    def roles():
        return [load_role(n, root).model_dump() for n in list_available("roles", root)]

    @app.post("/api/config")
    def set_config(req: ConfigReq):
        cfg = load_project_config(root)
        for k, v in req.model_dump().items():
            if v is not None:
                setattr(cfg, k, v)
        save_project_config(root, cfg)
        return cfg.model_dump()

    @app.post("/api/plan")
    def plan(req: PlanReq):
        def fn():
            g = _make_guild(req.profile)
            try:
                return g.plan(req.goal, extra_context=(f"ADDITIONAL BRIEF:\n{req.context}" if req.context else ""))
            finally:
                g.close()
        hub.start("plan", fn)
        return {"ok": True}

    @app.post("/api/run")
    def run(req: RunReq):
        def fn():
            g = _make_guild(req.profile)
            cfg = g.cfg
            roles_ = [r for r in cfg.roles_enabled if r not in set(req.skip)]
            outcomes = []
            try:
                p = g.load_plan()
                if p is None:
                    raise RuntimeError("no plan yet")
                while True:
                    task = p.get(req.task_id) if req.task_id else p.next_task()
                    if task is None:
                        break
                    out = g.run_task(p, task, roles=roles_)
                    outcomes.append(out)
                    if req.task_id or not req.all or not out.accepted:
                        break
            finally:
                g.close()
            return outcomes
        hub.start("run", fn)
        return {"ok": True}

    @app.post("/api/ask")
    def ask(req: AskReq):
        def fn():
            g = _make_guild(req.profile)
            try:
                return g.ask(req.role, req.question)
            finally:
                g.close()
        hub.start("ask", fn)
        return {"ok": True}

    @app.post("/api/plan/task")
    def update_task(body: dict):
        """Edit a task's fields or delete it (body: {id, ...fields} or {id, delete: true})."""
        plan = _read_plan(root)
        if plan is None:
            raise HTTPException(404, "no plan")
        plan_p = root / GUILD_DIR / "plan.json"
        tid = body.get("id")
        if body.get("delete"):
            plan["tasks"] = [t for t in plan["tasks"] if t["id"] != tid]
        else:
            for t in plan["tasks"]:
                if t["id"] == tid:
                    for k in ("title", "description", "done_when", "status", "files", "depends_on"):
                        if k in body:
                            t[k] = body[k]
        tmp = plan_p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(plan, indent=2), encoding="utf-8")
        os.replace(tmp, plan_p)
        return plan

    @app.get("/api/runs")
    def runs():
        out = []
        for p in sorted((root / GUILD_DIR / "runs").glob("*/trace.jsonl"), reverse=True)[:50]:
            ev = read_trace(p)
            calls = [e for e in ev if e["kind"] == "model_call"]
            start = next((e for e in ev if e["kind"] == "run_start"), {})
            out.append({"id": p.parent.name, "profile": start.get("profile"), "calls": len(calls),
                        "input_tokens": sum(e["input_tokens"] for e in calls),
                        "output_tokens": sum(e["output_tokens"] for e in calls),
                        "usd": round(sum(e.get("usd", 0) for e in calls), 4),
                        "phases": [e.get("phase") for e in ev if e["kind"] == "phase"][:12],
                        "ts": ev[0]["ts"] if ev else 0})
        return out

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: str):
        p = root / GUILD_DIR / "runs" / run_id / "trace.jsonl"
        if not p.exists() or "/" in run_id or ".." in run_id:
            raise HTTPException(404)
        return read_trace(p)

    @app.get("/api/events")
    def events():
        q = hub.subscribe()

        def gen():
            try:
                yield "retry: 2000\n\n"
                while True:
                    try:
                        ev = q.get(timeout=15)
                        yield f"data: {json.dumps(ev, default=str)}\n\n"
                    except queue.Empty:
                        yield ": keepalive\n\n"
            finally:
                hub.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app


def _read_plan(root: Path) -> dict | None:
    p = root / GUILD_DIR / "plan.json"
    for _ in range(3):
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            time.sleep(0.05)
    return None


def _ollama_models() -> list[str] | None:
    import httpx
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    try:
        r = httpx.get(f"{host}/api/tags", timeout=3)
        return [m["name"] for m in r.json().get("models", [])]
    except Exception:  # noqa: BLE001
        return None


def serve(root: Path, port: int = 7331, open_browser: bool = True) -> None:
    import uvicorn
    if not (root / GUILD_DIR / "config.yaml").exists():
        save_project_config(root, ProjectConfig())
    app = create_app(root)
    url = f"http://127.0.0.1:{port}"
    if open_browser:
        import webbrowser
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print(f"guild ui → {url}   (project: {root})   Ctrl+C to stop")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
