"""Local web dashboard: `guild ui`.

Single-process FastAPI app. Long jobs (plan / run / ask) execute on one background thread;
their trace events and progress messages are pushed to the browser over Server-Sent Events.
Binds to 127.0.0.1 only — this is a local tool, not a hosted service.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from ..config import (
    GUILD_DIR,
    ProjectConfig,
    list_available,
    load_profile,
    load_project_config,
    load_role,
    save_project_config,
)
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
        self._tok_buf: list[str] = []
        self._tok_role = ""
        self._tok_last = 0.0

    def publish(self, ev: dict) -> None:
        ev.setdefault("ts", time.time())
        self.flush_tokens()  # keep ordering: pending tokens go out before the next event
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

    def reset(self, root: Path) -> None:
        """Point the hub at another project. Subscribers (open SSE streams) are kept, so the
        browser keeps receiving events after a project switch; history is per project."""
        with self.lock:
            self.root = root
            self.job = None
            self.history = []
            self._tok_buf.clear()
            self._tok_role = ""
        self.publish({"kind": "project_switched", "root": str(root)})

    # -- streaming tokens: coalesce into ~80ms batches so the browser isn't flooded
    def token(self, role: str, chunk: str) -> None:
        with self.lock:
            if self._tok_role != role:
                self._flush_tokens_locked()
                self._tok_role = role
            self._tok_buf.append(chunk)
            due = time.time() - self._tok_last >= 0.08
        if due:
            self.flush_tokens()

    def flush_tokens(self) -> None:
        with self.lock:
            self._flush_tokens_locked()

    def _flush_tokens_locked(self) -> None:
        if not self._tok_buf:
            return
        text = "".join(self._tok_buf)
        self._tok_buf.clear()
        self._tok_last = time.time()
        ev = {"kind": "token", "role": self._tok_role, "text": text, "ts": time.time()}
        for q in list(self.subscribers):
            q.put(ev)  # tokens are not kept in history — they're ephemeral

    def start(self, kind: str, fn) -> Job:
        with self.lock:  # check-and-set under the lock: two clicks must not start two jobs
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
            except Exception as e:
                import traceback

                tb = traceback.format_exc()
                job.status = "error"
                job.error = f"{type(e).__name__}: {e}"
                print(tb, file=sys.stderr)
                self.publish(
                    {"kind": "job_error", "job": kind, "error": job.error, "traceback": tb[-3000:]}
                )

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
    task_ids: list[str] = []
    all: bool = False
    skip: list[str] = []
    profile: str | None = None


class AskReq(BaseModel):
    role: str
    question: str
    profile: str | None = None


class ChatReq(BaseModel):
    message: str
    profile: str | None = None


class ReviseReq(BaseModel):
    instruction: str
    profile: str | None = None


class ProjectReq(BaseModel):
    path: str


class ConfigReq(BaseModel):
    profile: str | None = None
    test_command: str | None = None
    sandbox: str | None = None
    roles_enabled: list[str] | None = None


class _State:
    """Mutable server state: the active project and its event hub (switchable at runtime)."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.hub = Hub(self.root)
        self.chat_history: list[dict] = []

    def switch(self, root: Path) -> None:
        self.root = root.resolve()
        self.hub.reset(self.root)  # same Hub object: open SSE connections stay subscribed
        self.chat_history = []


def create_app(root: Path) -> FastAPI:
    app = FastAPI(title="guild", docs_url=None, redoc_url=None)
    S = _State(root)

    def _make_guild(profile_override: str | None):
        from ..workflow import Guild

        cfg = load_project_config(S.root)
        prof = load_profile(profile_override or cfg.profile, S.root)
        g = Guild(
            S.root,
            cfg,
            prof,
            report=lambda lvl, msg: S.hub.publish({"kind": "report", "level": lvl, "msg": msg}),
            on_token=S.hub.token,
        )
        g.trace.echo = lambda ev: S.hub.publish(dict(ev))
        return g

    @app.get("/", response_class=HTMLResponse)
    def index():
        return resources.files("guild.ui").joinpath("index.html").read_text(encoding="utf-8")

    @app.get("/api/state")
    def state():
        # snapshot job status BEFORE reading files, so "not busy" implies the plan is on disk
        busy = S.hub.busy()
        job = _jsonable(S.hub.job) if S.hub.job else None
        cfg = load_project_config(S.root)
        plan = _read_plan(S.root)
        warnings: list[str] = []
        if plan:
            from ..workflow import Plan

            try:
                warnings = Plan.from_dict(plan).warnings()
            except (KeyError, TypeError):
                warnings = []
        return {
            "root": str(S.root),
            "config": cfg.model_dump(),
            "plan_warnings": warnings,
            "profiles": list_available("profiles", S.root),
            "roles": list_available("roles", S.root),
            "plan": plan,
            "busy": busy,
            "job": job,
        }

    # ---- project selection -------------------------------------------------------------
    @app.get("/api/project")
    def project_info():
        return {
            "root": str(S.root),
            "initialised": (S.root / GUILD_DIR / "config.yaml").exists(),
            "git": (S.root / ".git").is_dir(),
            "recent": _recent_projects(),
        }

    @app.post("/api/project")
    def project_switch(req: ProjectReq):
        if S.hub.busy():
            raise HTTPException(409, "a job is running; wait for it to finish before switching")
        p = Path(req.path).expanduser()
        if not p.is_dir():
            raise HTTPException(400, f"not a directory: {req.path}")
        S.switch(p)
        if not (S.root / GUILD_DIR / "config.yaml").exists():
            # first time here: write a default config (user can run `guild init` for the wizard)
            from ..hardware import detect_test_command

            save_project_config(S.root, ProjectConfig(test_command=detect_test_command(S.root)))
        _remember_project(S.root)
        return project_info()

    @app.get("/api/fs")
    def fs_list(path: str | None = None):
        """Server-side folder picker: list subdirectories of a path (or drives/home if none)."""
        if not path:
            entries = []
            if os.name == "nt":
                import string

                for d in string.ascii_uppercase:
                    if Path(f"{d}:\\").exists():
                        entries.append({"name": f"{d}:\\", "path": f"{d}:\\"})
            entries.insert(0, {"name": "~ (home)", "path": str(Path.home())})
            return {"path": "", "parent": None, "dirs": entries}
        p = Path(path).expanduser()
        if not p.is_dir():
            raise HTTPException(400, "not a directory")
        dirs = []
        try:
            for child in sorted(p.iterdir(), key=lambda c: c.name.lower()):
                if (
                    child.is_dir()
                    and not child.name.startswith(".")
                    and child.name
                    not in {
                        "node_modules",
                        "__pycache__",
                        "$RECYCLE.BIN",
                        "System Volume Information",
                    }
                ):
                    dirs.append(
                        {
                            "name": child.name,
                            "path": str(child),
                            "git": (child / ".git").is_dir(),
                            "guild": (child / GUILD_DIR).is_dir(),
                        }
                    )
        except PermissionError:
            pass
        return {
            "path": str(p),
            "parent": str(p.parent) if p.parent != p else None,
            "dirs": dirs[:500],
        }

    # ---- files -----------------------------------------------------------------------------
    @app.get("/api/files")
    def files():
        cfg = load_project_config(S.root)
        from ..tools.registry import ToolContext

        ctx = ToolContext(
            root=S.root, ignore=cfg.ignore, test_command="", lint_command=None, sandbox=None
        )
        out = []
        for dirpath, dirnames, filenames in os.walk(S.root):
            rel_dir = os.path.relpath(dirpath, S.root)
            dirnames[:] = sorted(
                d for d in dirnames if not ctx.is_ignored(os.path.join(rel_dir, d))
            )
            for f in sorted(filenames):
                rel = os.path.normpath(os.path.join(rel_dir, f)).replace("\\", "/")
                if rel.startswith("./"):
                    rel = rel[2:]
                if ctx.is_ignored(rel):
                    continue
                try:
                    size = (Path(dirpath) / f).stat().st_size
                except OSError:
                    size = 0
                out.append({"path": rel, "size": size})
                if len(out) >= 3000:
                    return {"root": str(S.root), "files": out, "truncated": True}
        return {"root": str(S.root), "files": out, "truncated": False}

    @app.get("/api/file")
    def file_read(path: str):
        from ..tools.registry import ToolContext

        cfg = load_project_config(S.root)
        ctx = ToolContext(
            root=S.root, ignore=cfg.ignore, test_command="", lint_command=None, sandbox=None
        )
        try:
            p = ctx.resolve(path)
        except PermissionError as e:
            raise HTTPException(403, str(e)) from e
        if not p.is_file():
            raise HTTPException(404, "no such file")
        if p.stat().st_size > 2_000_000:
            return {"path": path, "content": None, "binary": False, "too_large": True}
        raw = p.read_bytes()
        if b"\x00" in raw[:4096]:
            return {"path": path, "content": None, "binary": True}
        return {"path": path, "content": raw.decode("utf-8", errors="replace"), "binary": False}

    # ---- chat ------------------------------------------------------------------------------
    @app.get("/api/chat")
    def chat_history():
        return S.chat_history

    @app.delete("/api/chat")
    def chat_clear():
        S.chat_history = []
        return []

    @app.post("/api/chat")
    def chat(req: ChatReq):
        history = list(S.chat_history)
        S.chat_history.append({"role": "user", "content": req.message, "ts": time.time()})

        def fn():
            g = _make_guild(req.profile)
            try:
                res = g.chat(req.message, history)
            finally:
                g.close()
            S.chat_history.append(
                {"role": "assistant", "content": res.raw, "model": res.model, "ts": time.time()}
            )
            return {"text": res.raw, "model": res.model}

        S.hub.start("chat", fn)
        return {"ok": True}

    # ---- plan revision ---------------------------------------------------------------------
    @app.post("/api/plan/revise")
    def plan_revise(req: ReviseReq):
        def fn():
            g = _make_guild(req.profile)
            try:
                return g.revise_plan(req.instruction)
            finally:
                g.close()

        S.hub.start("revise", fn)
        return {"ok": True}

    @app.get("/api/doctor")
    def doctor(profile: str | None = None):
        cfg = load_project_config(S.root)
        prof = load_profile(profile or cfg.profile, S.root)
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
                    ok = (
                        ep.key_env is None
                        or bool(os.environ.get(ep.key_env))
                        or prefix in {"omniroute", "litellm", "custom"}
                    )
                    st = "configured" if ok else f"{ep.key_env} missing"
                else:
                    st, ok = "unknown provider", False
                rows.append({"slot": slot, "model": m, "status": st, "ok": ok})
        return {
            "profile": prof.model_dump(),
            "rows": rows,
            "git": (S.root / ".git").is_dir(),
            "ollama_models": ollama or [],
        }

    @app.get("/api/roles")
    def roles():
        return [load_role(n, S.root).model_dump() for n in list_available("roles", S.root)]

    @app.post("/api/config")
    def set_config(req: ConfigReq):
        cfg = load_project_config(S.root)
        for k, v in req.model_dump().items():
            if v is not None:
                setattr(cfg, k, v)
        save_project_config(S.root, cfg)
        return cfg.model_dump()

    @app.post("/api/plan")
    def plan(req: PlanReq):
        def fn():
            g = _make_guild(req.profile)
            try:
                return g.plan(
                    req.goal,
                    extra_context=(f"ADDITIONAL BRIEF:\n{req.context}" if req.context else ""),
                )
            finally:
                g.close()

        S.hub.start("plan", fn)
        return {"ok": True}

    @app.post("/api/run")
    def run(req: RunReq):
        def fn():
            g = _make_guild(req.profile)
            cfg = g.cfg
            roles_ = [r for r in cfg.roles_enabled if r not in set(req.skip)]
            try:
                p = g.load_plan()
                if p is None:
                    raise RuntimeError("no plan yet")
                ids = req.task_ids or ([req.task_id] if req.task_id else None)
                return g.run_tasks(p, ids, all_tasks=req.all, roles=roles_)
            finally:
                g.close()

        S.hub.start("run", fn)
        return {"ok": True}

    @app.post("/api/ask")
    def ask(req: AskReq):
        def fn():
            g = _make_guild(req.profile)
            try:
                return g.ask(req.role, req.question)
            finally:
                g.close()

        S.hub.start("ask", fn)
        return {"ok": True}

    @app.post("/api/plan/task")
    def update_task(body: dict):
        """Edit a task's fields or delete it (body: {id, ...fields} or {id, delete: true})."""
        plan = _read_plan(S.root)
        if plan is None:
            raise HTTPException(404, "no plan")
        plan_p = S.root / GUILD_DIR / "plan.json"
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

    # ---- branches: review & merge from the dashboard (human decision, guild never auto-merges)
    def _wf():
        from ..workflow import Guild

        cfg = load_project_config(S.root)
        prof = load_profile(cfg.profile, S.root)
        g = Guild.__new__(Guild)  # git helpers only; no trace/run dir created
        g.root, g.cfg, g.profile, g.dry_run = S.root, cfg, prof, False
        return g

    @app.get("/api/branches/{task_id}/diff")
    def branch_diff(task_id: str):
        plan = _read_plan(S.root) or {}
        task = next((t for t in plan.get("tasks", []) if t["id"] == task_id), None)
        if not task or not task.get("branch"):
            raise HTTPException(404, "task has no branch")
        return _wf().branch_diff(task["branch"])

    @app.post("/api/branches/{task_id}/merge")
    def branch_merge(task_id: str):
        if S.hub.busy():
            raise HTTPException(409, "a job is running")
        plan = _read_plan(S.root) or {}
        task = next((t for t in plan.get("tasks", []) if t["id"] == task_id), None)
        if not task or not task.get("branch"):
            raise HTTPException(404, "task has no branch")
        res = _wf().merge_branch(task["branch"])
        if res.get("ok"):
            task["merged"] = True
            plan_p = S.root / GUILD_DIR / "plan.json"
            tmp = plan_p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(plan, indent=2), encoding="utf-8")
            os.replace(tmp, plan_p)
            S.hub.publish(
                {
                    "kind": "report",
                    "level": "info",
                    "msg": f"merged {task['branch']} into {res['base']}",
                }
            )
        return res

    @app.post("/api/branches/{task_id}/discard")
    def branch_discard(task_id: str):
        if S.hub.busy():
            raise HTTPException(409, "a job is running")
        plan = _read_plan(S.root) or {}
        task = next((t for t in plan.get("tasks", []) if t["id"] == task_id), None)
        if not task or not task.get("branch"):
            raise HTTPException(404, "task has no branch")
        res = _wf().discard_branch(task["branch"])
        if res.get("ok"):
            task["branch"] = None
            task["status"] = "todo"
            plan_p = S.root / GUILD_DIR / "plan.json"
            tmp = plan_p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(plan, indent=2), encoding="utf-8")
            os.replace(tmp, plan_p)
        return res

    @app.get("/api/runs")
    def runs():
        out = []
        paths = sorted(
            (S.root / GUILD_DIR / "runs").glob("*/trace.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for p in paths[:50]:
            ev = read_trace(p)
            calls = [e for e in ev if e["kind"] == "model_call"]
            start = next((e for e in ev if e["kind"] == "run_start"), {})
            out.append(
                {
                    "id": p.parent.name,
                    "profile": start.get("profile"),
                    "calls": len(calls),
                    "input_tokens": sum(e["input_tokens"] for e in calls),
                    "output_tokens": sum(e["output_tokens"] for e in calls),
                    "usd": round(sum(e.get("usd", 0) for e in calls), 4),
                    "phases": [e.get("phase") for e in ev if e["kind"] == "phase"][:12],
                    "ts": ev[0]["ts"] if ev else 0,
                }
            )
        return out

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: str):
        if "/" in run_id or ".." in run_id or "\\" in run_id:
            raise HTTPException(404)
        p = S.root / GUILD_DIR / "runs" / run_id / "trace.jsonl"
        if not p.exists():
            raise HTTPException(404)
        events = read_trace(p)
        by_role: dict[str, dict] = {}
        for e in events:
            if e["kind"] == "model_call":
                r = by_role.setdefault(
                    e["role"],
                    {
                        "calls": 0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "usd": 0.0,
                        "latency_s": 0.0,
                        "models": set(),
                    },
                )
                r["calls"] += 1
                r["input_tokens"] += e["input_tokens"]
                r["output_tokens"] += e["output_tokens"]
                r["usd"] += e.get("usd", 0)
                r["latency_s"] += e.get("latency_s", 0)
                r["models"].add(e["model"])
        for r in by_role.values():
            r["models"] = sorted(r["models"])
            r["usd"] = round(r["usd"], 5)
            r["latency_s"] = round(r["latency_s"], 1)
        return {
            "events": events,
            "by_role": by_role,
            "started": events[0]["ts"] if events else 0,
            "ended": events[-1]["ts"] if events else 0,
        }

    @app.get("/api/events")
    def events():
        q = S.hub.subscribe()

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
                S.hub.unsubscribe(q)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


_RECENT = Path.home() / ".guild" / "recent.json"


def _recent_projects() -> list[str]:
    try:
        items = json.loads(_RECENT.read_text(encoding="utf-8"))
        return [p for p in items if Path(p).is_dir()][:12]
    except (OSError, ValueError):
        return []


def _remember_project(root: Path) -> None:
    items = [str(root)] + [p for p in _recent_projects() if p != str(root)]
    try:
        _RECENT.parent.mkdir(parents=True, exist_ok=True)
        _RECENT.write_text(json.dumps(items[:12]), encoding="utf-8")
    except OSError:
        pass


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
    except Exception:
        return None


def serve(root: Path, port: int = 7331, open_browser: bool = True) -> None:
    import uvicorn

    if not (root / GUILD_DIR / "config.yaml").exists():
        save_project_config(root, ProjectConfig())
    _remember_project(root.resolve())
    app = create_app(root)
    url = f"http://127.0.0.1:{port}"
    if open_browser:
        import webbrowser

        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print(f"guild ui → {url}   (project: {root})   Ctrl+C to stop")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
