"""Local web dashboard: `guild ui`.

Single-process FastAPI app. Long jobs (plan / run / ask) execute on one background thread;
their trace events and progress messages are pushed to the browser over Server-Sent Events.
Binds to 127.0.0.1 only — this is a local tool, not a hosted service.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
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
from ..providers import AllProvidersFailed, BudgetExceeded, Cancelled
from ..providers.openai_compat import ENDPOINTS
from ..trace import read_trace

# --------------------------------------------------------------------------- job runner


@dataclass
class Job:
    kind: str
    status: str = "running"  # running | done | error | cancelled
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
            except Cancelled:
                job.status = "cancelled"
                job.error = "cancelled"
                self.publish({"kind": "job_cancelled", "job": kind})
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


class PairReq(BaseModel):
    message: str
    mode: str = "ask"  # ask | auto-edits | plan
    profile: str | None = None


class ApproveReq(BaseModel):
    approve: bool
    reason: str = ""
    remember: bool = False  # also approve the remaining file edits of this turn


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

    CHAT_KEEP = 200

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.hub = Hub(self.root)
        self.active = None  # the Guild of the running job, for /api/cancel
        self.approvals: dict[str, dict] = {}  # pending approval requests of a pair turn
        self.auto_edits = False  # "approve all edits" for the current pair turn
        self.chat_history: list[dict] = self._load_chat()

    def switch(self, root: Path) -> None:
        self.root = root.resolve()
        self.hub.reset(self.root)  # same Hub object: open SSE connections stay subscribed
        self.active = None
        self.approvals = {}
        self.chat_history = self._load_chat()

    # chat survives a restart: .guild/chat.jsonl (inside .guild, so never committed by guild
    # and not writable by any agent)
    def _chat_path(self) -> Path:
        return self.root / GUILD_DIR / "chat.jsonl"

    def _load_chat(self) -> list[dict]:
        items: list[dict] = []
        try:
            with open(self._chat_path(), encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(item, dict) and item.get("role") in ("user", "assistant"):
                        items.append(item)
        except OSError:
            pass
        return items[-self.CHAT_KEEP :]

    def add_chat(self, **entry: Any) -> None:
        entry.setdefault("ts", time.time())
        self.chat_history.append(entry)
        try:
            self._chat_path().parent.mkdir(parents=True, exist_ok=True)
            with open(self._chat_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass  # history stays in memory

    def clear_chat(self) -> None:
        self.chat_history = []
        with contextlib.suppress(OSError):
            self._chat_path().unlink()


LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
TASK_STATUSES = frozenset({"todo", "in_progress", "done", "blocked"})


def _hostname(value: str) -> str:
    """Host part of a Host header / netloc: 'localhost:7331' -> 'localhost', '[::1]:80' -> '::1'."""
    v = value.strip().lower()
    if v.startswith("["):
        return v[1 : v.find("]")] if "]" in v else v
    return v.rsplit(":", 1)[0] if v.count(":") == 1 else v


def _write_plan(root: Path, plan: dict) -> None:
    plan_p = root / GUILD_DIR / "plan.json"
    tmp = plan_p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    os.replace(tmp, plan_p)


def create_app(root: Path, allowed_hosts: set[str] | None = None) -> FastAPI:
    app = FastAPI(title="guild", docs_url=None, redoc_url=None)
    S = _State(root)
    extra = {h.strip().lower() for h in os.environ.get("GUILD_UI_HOSTS", "").split(",")}
    hosts = (set(LOCAL_HOSTS) | set(allowed_hosts or ()) | extra) - {""}

    @app.middleware("http")
    async def _local_only(request: Request, call_next):
        """Binding to 127.0.0.1 is not enough: a web page the user visits can still aim their
        browser at this port (DNS rebinding, cross-site POST). The dashboard can run commands
        and merge branches, so only requests addressed to — and coming from — localhost pass."""
        if _hostname(request.headers.get("host", "")) not in hosts:
            return JSONResponse({"detail": "guild ui only answers on localhost"}, 403)
        origin = request.headers.get("origin")
        if origin and request.method not in ("GET", "HEAD", "OPTIONS"):
            from urllib.parse import urlsplit

            if (urlsplit(origin).hostname or "").lower() not in hosts:
                return JSONResponse({"detail": "cross-site request refused"}, 403)
        return await call_next(request)

    def _busy_with_plan() -> bool:
        return (
            S.hub.busy() and S.hub.job is not None and S.hub.job.kind in ("run", "plan", "revise")
        )

    def _task_or_404(plan: dict | None, task_id: str) -> dict:
        tasks = (plan or {}).get("tasks", [])
        task = next((t for t in tasks if isinstance(t, dict) and t.get("id") == task_id), None)
        if task is None:
            raise HTTPException(404, f"no task {task_id}")
        return task

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
        S.active = g
        return g

    def _public(entry: dict) -> dict:
        return {k: v for k, v in entry.items() if k not in ("event", "decision")}

    def _approver(req: dict) -> tuple[bool, str]:
        """Called on the job thread before a pair-mode change: publish the request, then wait
        for POST /api/approve/<id> (or a cancel)."""
        if S.auto_edits and req.get("kind") == "edit":
            return True, "approved for this turn"
        g = S.active
        entry = {"id": uuid.uuid4().hex[:12], **req, "event": threading.Event(), "decision": None}
        S.approvals[entry["id"]] = entry
        # the request keeps its own "kind" (edit | command), so it travels as a sub-object
        S.hub.publish({"kind": "approval_request", "request": _public(entry)})
        try:
            while not entry["event"].wait(0.2):
                if g is not None and g.cancel_event.is_set():
                    raise Cancelled()
        finally:
            S.approvals.pop(entry["id"], None)
        approved, why = entry["decision"]
        return approved, why

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
            "pending_approvals": [_public(e) for e in list(S.approvals.values())],
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
        S.clear_chat()
        return []

    @app.post("/api/chat")
    def chat(req: ChatReq):
        if S.hub.busy():  # before touching the history: a refused message must not linger in it
            raise HTTPException(409, f"a {S.hub.job.kind} job is already running")
        if not req.message.strip():
            raise HTTPException(400, "empty message")
        history = list(S.chat_history)
        S.add_chat(role="user", content=req.message)

        def fn():
            g = _make_guild(req.profile)
            try:
                res = g.chat(req.message, history)
            finally:
                g.close()
            S.add_chat(role="assistant", content=res.raw, model=res.model)
            return {"text": res.raw, "model": res.model}

        S.hub.start("chat", fn)
        return {"ok": True}

    # ---- pair programming: the chat can change the project, behind an approval gate ----------
    @app.post("/api/pair")
    def pair(req: PairReq):
        from ..workflow import PAIR_MODES

        if S.hub.busy():
            raise HTTPException(409, f"a {S.hub.job.kind} job is already running")
        if not req.message.strip():
            raise HTTPException(400, "empty message")
        if req.mode not in PAIR_MODES:
            raise HTTPException(400, f"mode must be one of {list(PAIR_MODES)}")
        history = list(S.chat_history)
        S.add_chat(role="user", content=req.message, mode=req.mode)
        S.auto_edits = False

        def fn():
            g = _make_guild(req.profile)
            try:
                res = g.pair(req.message, history, mode=req.mode, approver=_approver)
            finally:
                g.close()
            files = (res.data or {}).get("files_changed", [])
            S.add_chat(
                role="assistant", content=res.raw, model=res.model, mode=req.mode, files=files
            )
            return {"text": res.raw, "model": res.model, "files_changed": files, "mode": req.mode}

        S.hub.start("pair", fn)
        return {"ok": True}

    @app.post("/api/approve/{approval_id}")
    def approve(approval_id: str, req: ApproveReq):
        entry = S.approvals.get(approval_id)
        if entry is None or entry["decision"] is not None:
            raise HTTPException(404, "no such pending approval (already answered or cancelled)")
        if req.approve and req.remember and entry.get("kind") == "edit":
            S.auto_edits = True
        entry["decision"] = (req.approve, req.reason.strip()[:300])
        entry["event"].set()
        S.hub.publish({"kind": "approval_resolved", "id": approval_id, "approved": req.approve})
        return {"ok": True}

    @app.post("/api/cancel")
    def cancel():
        """Stop the running job at its next safe point (model call, tool call, running command,
        pending approval). A cancelled task goes back to "todo"."""
        g = S.active
        if not S.hub.busy() or g is None:
            return {"ok": False, "detail": "no job is running"}
        g.cancel()
        return {"ok": True, "job": S.hub.job.kind}

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
        if req.profile is not None and req.profile not in list_available("profiles", S.root):
            raise HTTPException(400, f"unknown profile '{req.profile}'")
        unknown = set(req.roles_enabled or ()) - set(list_available("roles", S.root))
        if unknown:
            raise HTTPException(400, f"unknown roles: {', '.join(sorted(unknown))}")
        if req.test_command is not None and not req.test_command.strip():
            raise HTTPException(400, "test_command must not be empty")
        try:
            for k, v in req.model_dump().items():
                if v is not None:
                    setattr(cfg, k, v)  # validated on assignment
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
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
        if _busy_with_plan():
            # the running job holds the plan in memory and would overwrite this edit (or this
            # edit would overwrite the task statuses it is saving)
            raise HTTPException(409, "the plan is in use by a running job; try again when it ends")
        plan = _read_plan(S.root)
        if plan is None:
            raise HTTPException(404, "no plan")
        tid = body.get("id")
        task = _task_or_404(plan, tid)
        if body.get("delete"):
            plan["tasks"] = [t for t in plan["tasks"] if t is not task]
            for t in plan["tasks"]:  # nothing may keep waiting for a task that is gone
                if isinstance(t.get("depends_on"), list):
                    t["depends_on"] = [d for d in t["depends_on"] if d != tid]
        else:
            ids = {t.get("id") for t in plan["tasks"]}
            for k in ("title", "description", "done_when"):
                if k in body:
                    if not isinstance(body[k], str):
                        raise HTTPException(400, f"{k} must be a string")
                    task[k] = body[k]
            if "status" in body:
                if body["status"] not in TASK_STATUSES:
                    raise HTTPException(400, f"status must be one of {sorted(TASK_STATUSES)}")
                task["status"] = body["status"]
            for k in ("files", "depends_on"):
                if k in body:
                    v = body[k]
                    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                        raise HTTPException(400, f"{k} must be a list of strings")
                    task[k] = [d for d in v if d in ids and d != tid] if k == "depends_on" else v
        _write_plan(S.root, plan)
        return plan

    # ---- branches: review & merge from the dashboard (human decision, guild never auto-merges)
    def _wf():
        from ..workflow import Guild

        g = Guild.__new__(Guild)  # git helpers only; no trace/run dir created
        # no profile needed for git work: a broken profile must not block diff/merge/discard
        g.root, g.cfg, g.profile, g.dry_run = S.root, load_project_config(S.root), None, False
        g.report = lambda lvl, msg: None
        return g

    @app.get("/api/branches/{task_id}/diff")
    def branch_diff(task_id: str):
        task = _task_or_404(_read_plan(S.root), task_id)
        if not task.get("branch"):
            raise HTTPException(404, "task has no branch")
        return _wf().branch_diff(task["branch"], task.get("base"))

    @app.post("/api/branches/{task_id}/merge")
    def branch_merge(task_id: str):
        if S.hub.busy():
            raise HTTPException(409, "a job is running")
        plan = _read_plan(S.root)
        task = _task_or_404(plan, task_id)
        if not task.get("branch"):
            raise HTTPException(404, "task has no branch")
        # back into the branch the task started from, not blindly into main/master
        res = _wf().merge_branch(task["branch"], task.get("base"))
        if res.get("ok"):
            task["merged"] = True
            _write_plan(S.root, plan)
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
        plan = _read_plan(S.root)
        task = _task_or_404(plan, task_id)
        if not task.get("branch"):
            raise HTTPException(404, "task has no branch")
        res = _wf().discard_branch(task["branch"])
        if res.get("ok"):
            task["branch"] = None
            task["status"] = "todo"
            task["merged"] = False
            _write_plan(S.root, plan)
        return res

    @app.get("/api/runs")
    def runs():
        out = []

        def mtime(p: Path) -> float:
            try:
                return p.stat().st_mtime
            except OSError:
                return 0.0

        paths = sorted((S.root / GUILD_DIR / "runs").glob("*/trace.jsonl"), key=mtime, reverse=True)
        for p in paths[:50]:
            try:
                ev = read_trace(p)
            except OSError:
                continue  # run folder removed while listing
            calls = [e for e in ev if e["kind"] == "model_call"]
            start = next((e for e in ev if e["kind"] == "run_start"), {})
            out.append(
                {
                    "id": p.parent.name,
                    "profile": start.get("profile"),
                    "calls": len(calls),
                    "input_tokens": sum(e.get("input_tokens") or 0 for e in calls),
                    "output_tokens": sum(e.get("output_tokens") or 0 for e in calls),
                    "usd": round(sum(e.get("usd") or 0 for e in calls), 4),
                    "phases": [e.get("phase") for e in ev if e["kind"] == "phase"][:12],
                    "ts": ev[0].get("ts", 0) if ev else 0,
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
                    e.get("role") or "?",
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
                r["input_tokens"] += e.get("input_tokens") or 0
                r["output_tokens"] += e.get("output_tokens") or 0
                r["usd"] += e.get("usd") or 0
                r["latency_s"] += e.get("latency_s") or 0
                r["models"].add(str(e.get("model")))
        for r in by_role.values():
            r["models"] = sorted(r["models"])
            r["usd"] = round(r["usd"], 5)
            r["latency_s"] = round(r["latency_s"], 1)
        return {
            "events": events,
            "by_role": by_role,
            "started": events[0].get("ts", 0) if events else 0,
            "ended": events[-1].get("ts", 0) if events else 0,
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
