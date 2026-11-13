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
import string
import sys
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from ..config import (
    ConfigError,
    ProjectConfig,
    list_available,
    load_profile,
    load_project_config,
    load_role,
    save_project_config,
)
from ..errors import Cancelled
from ..hardware import detect_test_command
from ..paths import ProjectPaths
from ..plan import Plan, PlanError, PlanStore, Task, UnknownTask
from ..providers import AllProvidersFailed, BudgetExceeded
from ..providers.status import ollama_models, profile_status
from ..repo import Repo
from ..tools import Workspace
from ..trace import jsonable, read_trace, summarize
from ..trust import UntrustedProject, require_trust
from ..workflow import PAIR_MODES, Guild, PairSession

# --------------------------------------------------------------------------- job runner


@dataclass
class Job:
    kind: str
    status: str = "running"  # running | done | error | cancelled
    started: float = field(default_factory=time.time)
    result: Any = None
    error: str | None = None


SUBSCRIBER_BACKLOG = 5000  # events one event-stream reader may fall behind


class Hub:
    """One background worker + fan-out of events to any number of SSE subscribers.

    Subscribers get every trace event (see guild.trace) plus these, which are never written
    to a trace: token (streamed text, batched), report (level + msg for a log line),
    approval_request / approval_resolved (pair mode), job_done / job_error / job_cancelled,
    project_switched.
    """

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
            try:
                q.put_nowait(ev)
            except queue.Full:
                # a reader that stopped reading (a suspended browser tab): end its stream. It
                # reconnects by itself and is sent the recent history again.
                q.closed = True
                self.unsubscribe(q)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=SUBSCRIBER_BACKLOG)
        q.closed = False
        with self.lock:
            self.subscribers.append(q)
            for ev in self.history[-min(200, q.maxsize) :]:
                q.put_nowait(ev)
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

    def start(self, kind: str, fn, accepted: Callable[[], None] | None = None) -> Job:
        """Run `fn` on the worker thread. `accepted` runs first, only when the job is really
        going to start (a chat message is recorded then, never when the request is refused)."""
        with self.lock:  # check-and-set under the lock: two clicks must not start two jobs
            if self.busy():
                raise HTTPException(409, f"a {self.job.kind} job is already running")
            if accepted:
                accepted()
            job = Job(kind=kind)
            self.job = job

        def _run():
            try:
                job.result = fn()
                job.status = "done"
                self.publish({"kind": "job_done", "job": kind, "result": jsonable(job.result)})
            except Cancelled:
                job.status = "cancelled"
                job.error = "cancelled"
                self.publish({"kind": "job_cancelled", "job": kind})
            except (
                AllProvidersFailed,
                BudgetExceeded,
                RuntimeError,
                FileNotFoundError,
                ConfigError,
            ) as e:
                job.status = "error"
                job.error = str(e)
                self.publish({"kind": "job_error", "job": kind, "error": str(e)})
            except Exception as e:
                tb = traceback.format_exc()
                job.status = "error"
                job.error = f"{type(e).__name__}: {e}"
                print(tb, file=sys.stderr)
                self.publish(
                    {"kind": "job_error", "job": kind, "error": job.error, "traceback": tb[-3000:]}
                )

        threading.Thread(target=_run, daemon=True).start()
        return job


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
        self.hub = Hub(root.resolve())
        self._open(root)

    def _open(self, root: Path) -> None:
        self.root = root.resolve()
        self.paths = ProjectPaths(self.root)
        self.plans = PlanStore(self.paths.plan)
        self.repo = Repo(self.root)
        self.active: Guild | None = None  # the Guild of the running job, for /api/cancel
        self.cancel_requested = False  # a cancel that arrived before that Guild existed
        self.session = PairSession()  # the running pair turn's permissions
        self.approvals: dict[str, dict] = {}  # approval requests waiting for the owner
        self.chat_history = self._load_chat()

    def switch(self, root: Path) -> None:
        self._open(root)
        self.hub.reset(self.root)  # same Hub object: open SSE connections stay subscribed

    def config(self) -> ProjectConfig:
        return load_project_config(self.root)

    def workspace(self) -> Workspace:
        return Workspace(self.root, self.config().ignore)

    def plan(self) -> Plan:
        plan = self.plans.load()
        if plan is None:
            raise HTTPException(404, "no plan")
        return plan

    def _load_chat(self) -> list[dict]:
        items: list[dict] = []
        with contextlib.suppress(OSError), open(self.paths.chat, encoding="utf-8") as f:
            for line in f:
                with contextlib.suppress(json.JSONDecodeError):
                    item = json.loads(line)
                    if isinstance(item, dict) and item.get("role") in ("user", "assistant"):
                        items.append(item)
        return items[-self.CHAT_KEEP :]

    def add_chat(self, **entry: Any) -> None:
        entry.setdefault("ts", time.time())
        self.chat_history.append(entry)
        with contextlib.suppress(OSError):  # the history then lives in memory only
            self.paths.chat.parent.mkdir(parents=True, exist_ok=True)
            with open(self.paths.chat, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def clear_chat(self) -> None:
        self.chat_history = []
        with contextlib.suppress(OSError):
            self.paths.chat.unlink()


LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _hostname(value: str) -> str:
    """Host part of a Host header / netloc: 'localhost:7331' -> 'localhost', '[::1]:80' -> '::1'."""
    v = value.strip().lower()
    if v.startswith("["):
        return v[1 : v.find("]")] if "]" in v else v
    return v.rsplit(":", 1)[0] if v.count(":") == 1 else v


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
        changes_state = request.method not in ("GET", "HEAD", "OPTIONS")
        # same origin, port included: another program's page on localhost:3000 (a dev server
        # showing code an agent wrote) is as foreign to the dashboard as any web site
        same_origin = origin is not None and (
            urlsplit(origin).netloc.lower() == request.headers.get("host", "").strip().lower()
        )
        if origin and changes_state and not same_origin:
            return JSONResponse({"detail": "cross-site request refused"}, 403)
        return await call_next(request)

    @app.exception_handler(ConfigError)
    async def _config_error(request: Request, e: ConfigError):
        return JSONResponse({"detail": str(e)}, 400)

    @app.exception_handler(OSError)
    async def _os_error(request: Request, e: OSError):
        # a path the OS refuses (too long, not a directory, no permission) is the caller's
        # problem to read about, not a crash
        return JSONResponse({"detail": f"{type(e).__name__}: {e}"}, 400)

    @app.exception_handler(UnknownTask)
    async def _unknown_task(request: Request, e: UnknownTask):
        return JSONResponse({"detail": str(e)}, 404)

    @app.exception_handler(PlanError)
    async def _plan_error(request: Request, e: PlanError):
        return JSONResponse({"detail": str(e)}, 400)

    def _not_while_running(*kinds: str) -> None:
        job = S.hub.job
        if S.hub.busy() and (not kinds or job.kind in kinds):
            raise HTTPException(409, f"a {job.kind} job is running; try again when it ends")

    def _job(
        kind: str,
        profile: str | None,
        work: Callable[[Guild], Any],
        accepted: Callable[[], None] | None = None,
    ) -> dict:
        """Start a background job: `work` gets a Guild wired to the event stream."""

        def run():
            cfg = S.config()
            g = Guild(
                S.root,
                cfg,
                load_profile(profile or cfg.profile, S.root),
                report=lambda lvl, msg: S.hub.publish({"kind": "report", "level": lvl, "msg": msg}),
                on_token=S.hub.token,
                on_event=lambda ev: S.hub.publish(dict(ev)),
            )
            S.active = g
            try:
                if S.cancel_requested:  # the owner pressed stop while the job was starting
                    g.cancel()
                return work(g)
            finally:
                S.active = None
                S.cancel_requested = False
                g.close()

        S.hub.start(kind, run, accepted)
        return {"ok": True}

    def _branch_task(task_id: str) -> tuple[Plan, Task]:
        plan = S.plan()
        (task,) = plan.require(task_id)
        if not task.branch:
            raise HTTPException(404, "task has no branch")
        return plan, task

    def _public(entry: dict) -> dict:
        return {k: v for k, v in entry.items() if k not in ("event", "decision")}

    def _approver(req: dict) -> tuple[bool, str]:
        """Called on the job thread before a pair-mode change: publish the request, then wait
        for POST /api/approve/<id> (or a cancel)."""
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
        job = jsonable(S.hub.job) if S.hub.job else None
        try:
            plan = S.plans.load()
            warnings = plan.warnings() if plan else []
        except PlanError as e:
            plan, warnings = None, [str(e)]
        try:
            config, config_error = S.config(), None
        except ConfigError as e:
            # the page must still load to say what is wrong; jobs fail with the same message
            config, config_error = ProjectConfig(), str(e)
        return {
            "root": str(S.root),
            "config": config.model_dump(),
            "config_error": config_error or _untrusted(S.root),
            "plan_warnings": warnings,
            "profiles": list_available("profiles", S.root),
            "roles": list_available("roles", S.root),
            "plan": plan.to_dict() if plan else None,
            "busy": busy,
            "job": job,
            "pending_approvals": [_public(e) for e in list(S.approvals.values())],
        }

    # ---- project selection -------------------------------------------------------------
    @app.get("/api/project")
    def project_info():
        return {
            "root": str(S.root),
            "initialised": S.paths.initialised,
            "git": S.repo.exists,
            "recent": _recent_projects(),
        }

    @app.post("/api/project")
    def project_switch(req: ProjectReq):
        _not_while_running()
        S.switch(_folder(req.path))
        _ensure_config(S.root)
        _remember_project(S.root)
        return project_info()

    @app.get("/api/fs")
    def fs_list(path: str | None = None):
        """Server-side folder picker: list subdirectories of a path (or drives/home if none)."""
        if not path:
            entries = []
            if os.name == "nt":
                for d in string.ascii_uppercase:
                    if Path(f"{d}:\\").exists():
                        entries.append({"name": f"{d}:\\", "path": f"{d}:\\"})
            entries.insert(0, {"name": "~ (home)", "path": str(Path.home())})
            return {"path": "", "parent": None, "dirs": entries}
        p = _folder(path)
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
                            "git": ProjectPaths(child).has_git,
                            "guild": ProjectPaths(child).guild_dir.is_dir(),
                        }
                    )
        except PermissionError:
            pass  # a folder the OS will not list: show it as empty rather than fail the picker
        return {
            "path": str(p),
            "parent": str(p.parent) if p.parent != p else None,
            "dirs": dirs[:500],
        }

    # ---- files -----------------------------------------------------------------------------
    @app.get("/api/files")
    def files():
        ws = S.workspace()
        out = []
        for rel, p in ws.walk(S.root):
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            out.append({"path": rel.replace("\\", "/"), "size": size})
            if len(out) >= 3000:
                break
        return {"root": str(S.root), "files": out, "truncated": len(out) >= 3000}

    @app.get("/api/file")
    def file_read(path: str):
        try:
            p = Workspace(S.root, []).resolve(path)
        except PermissionError as e:
            raise HTTPException(403, str(e)) from e
        except ValueError as e:  # a NUL byte in the path
            raise HTTPException(400, str(e)) from e
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
        if not req.message.strip():
            raise HTTPException(400, "empty message")
        history = list(S.chat_history)

        def work(g: Guild):
            res = g.chat(req.message, history)
            S.add_chat(role="assistant", content=res.raw, model=res.model)
            return {"text": res.raw, "model": res.model}

        return _job("chat", req.profile, work, lambda: S.add_chat(role="user", content=req.message))

    # ---- pair programming: the chat can change the project, behind an approval gate ----------
    @app.post("/api/pair")
    def pair(req: PairReq):
        if not req.message.strip():
            raise HTTPException(400, "empty message")
        if req.mode not in PAIR_MODES:
            raise HTTPException(400, f"mode must be one of {list(PAIR_MODES)}")
        history = list(S.chat_history)

        def accepted():
            S.session = PairSession(req.mode)  # "approve all edits" lasts for one turn here
            S.add_chat(role="user", content=req.message, mode=req.mode)

        def work(g: Guild):
            res = g.pair(req.message, history, session=S.session, ask=_approver)
            files = res.data["files_changed"]
            S.add_chat(
                role="assistant", content=res.raw, model=res.model, mode=req.mode, files=files
            )
            return {"text": res.raw, "model": res.model, "files_changed": files, "mode": req.mode}

        return _job("pair", req.profile, work, accepted)

    @app.post("/api/approve/{approval_id}")
    def approve(approval_id: str, req: ApproveReq):
        with S.hub.lock:  # two windows answering at once: the first answer stands
            entry = S.approvals.get(approval_id)
            if entry is None or entry["decision"] is not None:
                raise HTTPException(404, "no such pending approval (already answered or cancelled)")
            entry["decision"] = (req.approve, req.reason.strip()[:300])
        if req.approve and req.remember and entry.get("kind") == "edit":
            S.session.all_edits = True
        entry["event"].set()
        S.hub.publish({"kind": "approval_resolved", "id": approval_id, "approved": req.approve})
        return {"ok": True}

    @app.post("/api/cancel")
    def cancel():
        """Stop the running job at its next safe point (model call, tool call, running command,
        pending approval). A cancelled task goes back to "todo"."""
        if not S.hub.busy():
            return {"ok": False, "detail": "no job is running"}
        g = S.active
        if g is None:
            S.cancel_requested = True
        else:
            g.cancel()
        return {"ok": True, "job": S.hub.job.kind}

    # ---- plan revision ---------------------------------------------------------------------
    @app.post("/api/plan/revise")
    def plan_revise(req: ReviseReq):
        return _job("revise", req.profile, lambda g: g.revise_plan(req.instruction))

    @app.get("/api/doctor")
    def doctor(profile: str | None = None):
        prof = load_profile(profile or S.config().profile, S.root)
        pulled = ollama_models()
        return {
            "profile": prof.model_dump(),
            "rows": [
                {"slot": m.slot, "model": m.model, "status": m.status, "ok": m.ok}
                for m in profile_status(prof, pulled)
            ],
            "git": S.repo.exists,
            "ollama_models": pulled or [],
        }

    @app.get("/api/roles")
    def roles():
        out = []
        for name in list_available("roles", S.root):
            # one unreadable role file must not hide the others; using that role reports it
            with contextlib.suppress(ConfigError):
                out.append(load_role(name, S.root).model_dump())
        return out

    @app.post("/api/config")
    def set_config(req: ConfigReq):
        cfg = S.config()
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
        brief = f"ADDITIONAL BRIEF:\n{req.context}" if req.context else ""
        return _job("plan", req.profile, lambda g: g.plan(req.goal, extra_context=brief))

    @app.post("/api/run")
    def run(req: RunReq):
        def work(g: Guild):
            plan = g.plans.load()
            if plan is None:
                raise RuntimeError("no plan yet")
            ids = req.task_ids or ([req.task_id] if req.task_id else None)
            roles = [r for r in g.cfg.roles_enabled if r not in req.skip]
            return g.run_tasks(plan, ids, all_tasks=req.all, roles=roles)

        return _job("run", req.profile, work)

    @app.post("/api/ask")
    def ask(req: AskReq):
        return _job("ask", req.profile, lambda g: g.ask(req.role, req.question))

    @app.post("/api/plan/task")
    def update_task(body: dict):
        """Edit a task's fields or delete it (body: {id, ...fields} or {id, delete: true})."""
        # a running job holds the plan in memory: it would overwrite this edit, or this edit
        # would overwrite the task statuses it saves
        _not_while_running("run", "plan", "revise")
        plan = S.plan()
        if body.get("delete"):
            plan.delete_task(body.get("id"))
        else:
            plan.edit_task(body.get("id"), body)
        S.plans.save(plan)
        return plan.to_dict()

    # ---- branches: review & merge from the dashboard (human decision, guild never auto-merges)
    @app.get("/api/branches/{task_id}/diff")
    def branch_diff(task_id: str):
        _, task = _branch_task(task_id)
        return S.repo.branch_diff(task.branch, task.base)

    @app.post("/api/branches/{task_id}/merge")
    def branch_merge(task_id: str):
        _not_while_running()
        plan, task = _branch_task(task_id)
        res = S.repo.merge_branch(task.branch, task.base)
        if res["ok"]:
            plan.branch_merged(task_id)
            S.plans.save(plan)
            msg = f"merged {task.branch} into {res['base']}"
            S.hub.publish({"kind": "report", "level": "info", "msg": msg})
        return res

    @app.post("/api/branches/{task_id}/discard")
    def branch_discard(task_id: str):
        _not_while_running()
        plan, task = _branch_task(task_id)
        res = S.repo.discard_branch(task.branch)
        if res["ok"]:
            plan.branch_discarded(task_id)
            S.plans.save(plan)
        return res

    @app.get("/api/runs")
    def runs():
        out = []
        for p in S.paths.traces()[:50]:
            try:
                events = read_trace(p)
            except OSError:
                continue  # run folder removed while listing
            totals = summarize(events)
            out.append(
                {
                    "id": p.parent.name,
                    **{k: totals[k] for k in ("profile", "calls", "input_tokens", "usd")},
                    "output_tokens": totals["output_tokens"],
                    "phases": totals["phases"][:12],
                    "ts": totals["started"],
                }
            )
        return out

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: str):
        if "/" in run_id or ".." in run_id or "\\" in run_id:
            raise HTTPException(404)
        p = S.paths.trace(run_id)
        if not p.exists():
            raise HTTPException(404)
        events = read_trace(p)
        totals = summarize(events)
        return {"events": events, **{k: totals[k] for k in ("by_role", "started", "ended")}}

    @app.get("/api/events")
    def events():
        q = S.hub.subscribe()

        def gen():
            try:
                yield "retry: 2000\n\n"
                while not (q.closed and q.empty()):
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


def _recent_projects() -> list[str]:
    try:
        items = json.loads(ProjectPaths.recent_projects().read_text(encoding="utf-8"))
        return [p for p in items if isinstance(p, str) and Path(p).is_dir()][:12]
    except (OSError, ValueError, TypeError):
        return []


def _remember_project(root: Path) -> None:
    items = [str(root)] + [p for p in _recent_projects() if p != str(root)]
    try:
        ProjectPaths.recent_projects().parent.mkdir(parents=True, exist_ok=True)
        ProjectPaths.recent_projects().write_text(json.dumps(items[:12]), encoding="utf-8")
    except OSError:
        pass


def _untrusted(root: Path) -> str | None:
    """Why jobs will refuse to start in this project, when its configuration came with the
    repository and has not been approved."""
    try:
        require_trust(root)
    except UntrustedProject as e:
        return str(e)
    return None


def _folder(path: str) -> Path:
    """A folder named by the browser, or a 400 that says why not."""
    try:
        p = Path(path).expanduser()
        ok = p.is_dir()
    except (OSError, ValueError, RuntimeError):  # too long, NUL byte, unknown ~user
        ok = False
    if not ok:
        raise HTTPException(400, f"not a directory: {path[:200]}")
    return p


def _ensure_config(root: Path) -> None:
    """First time in this folder: write a default config (`guild init` is the wizard)."""
    if not ProjectPaths(root).initialised:
        save_project_config(root, ProjectConfig(test_command=detect_test_command(root)))


def serve(root: Path, port: int = 7331, open_browser: bool = True) -> None:
    import uvicorn

    _ensure_config(root)
    app = create_app(root)
    _remember_project(root.resolve())
    url = f"http://127.0.0.1:{port}"
    if open_browser:
        import webbrowser

        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print(f"guild ui → {url}   (project: {root})   Ctrl+C to stop")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
