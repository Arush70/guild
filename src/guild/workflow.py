"""The team workflow.

plan(goal)      Lead → roadmap + tasks, saved to .guild/plan.json
run(task)       Engineer implements on a git branch
                  → Verifier runs tests
                  → Critic + Security review
                  → Engineer revises (bounded rounds; escalates model after N failures)
                  → Lead accepts / revises / replans
                  → Docs updates docs
                Human merges the branch (guild never merges to main by itself).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agent import Agent, AgentResult
from .config import GUILD_DIR, Profile, ProjectConfig, Role, load_role
from .providers import Cancelled, CostTracker, Router
from .providers.router import load_prices
from .sandbox import make_sandbox
from .tools import ToolContext
from .trace import Trace, new_run_id

# Interactive pair programming: what needs the owner's approval.
#   ask         every file change and every non-inspection command
#   auto-edits  file changes go through; commands still ask
#   plan        read-only: explore and propose, change nothing
PAIR_MODES = ("ask", "auto-edits", "plan")

# Roles that must never change the project, even if their YAML lists run_command.
READONLY_ROLES = frozenset({"critic", "security", "verifier", "assistant", "performance"})


@dataclass
class Task:
    id: str
    title: str
    description: str
    files: list[str] = field(default_factory=list)
    done_when: str = ""
    depends_on: list[str] = field(default_factory=list)
    status: str = "todo"  # todo | in_progress | done | blocked
    branch: str | None = None
    notes: str = ""
    merged: bool = False  # set by the dashboard after the human merges the branch
    base: str | None = None  # branch the task started from; the dashboard merges back into it


_VAGUE = (
    "designed",
    "documented",
    "works well",
    "is complete",
    "is ready",
    "looks good",
    "is implemented",
    "is created",
    "is done",
    "is added",
    "functional",
)


def vague_done_when(text: str) -> bool:
    """Heuristic: a done_when that names no test/command/file is probably not checkable."""
    t = (text or "").lower()
    concrete = (
        "test",
        "pytest",
        "pass",
        "exit",
        "returns",
        "assert",
        "exists",
        "contains",
        "==",
        "file",
        ".py",
        "command",
        "output",
    )
    return not any(k in t for k in concrete) or (
        any(v in t for v in _VAGUE) and not any(k in t for k in ("test", "pass"))
    )


# Caches created while guild runs tests must never end up in its commits (git pathspecs).
_COMMIT_EXCLUDES = (
    ":(exclude,glob)**/__pycache__/**",
    ":(exclude,glob)__pycache__/**",
    ":(exclude)*.pyc",
    ":(exclude,glob)**/.pytest_cache/**",
    ":(exclude,glob).pytest_cache/**",
    ":(exclude,glob)**/.ruff_cache/**",
    ":(exclude,glob).ruff_cache/**",
    ":(exclude,glob)**/.mypy_cache/**",
    ":(exclude,glob).mypy_cache/**",
)


def _slug(text: str, limit: int) -> str:
    """Lower-case, runs of anything but letters/digits become one '-': always a valid git ref
    component (model-written ids like "Task 1: set~up" are not)."""
    return re.sub(r"-{2,}", "-", "".join(c if c.isalnum() else "-" for c in str(text).lower()))[
        :limit
    ].strip("-")


def _files_in_diff(diff: str) -> list[str]:
    return sorted(set(re.findall(r"^diff --git a/.+? b/(.+)$", diff, flags=re.M)))


def _task_from_dict(t: dict) -> Task:
    known = Task.__dataclass_fields__
    return Task(**{k: v for k, v in t.items() if k in known})


@dataclass
class Plan:
    goal: str
    roadmap: list[str]
    tasks: list[Task]
    risks: list[str] = field(default_factory=list)
    questions_for_owner: list[str] = field(default_factory=list)
    created: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "goal": self.goal,
            "roadmap": self.roadmap,
            "risks": self.risks,
            "questions_for_owner": self.questions_for_owner,
            "created": self.created,
            "tasks": [t.__dict__ for t in self.tasks],
        }

    @classmethod
    def from_dict(cls, d: dict) -> Plan:
        return cls(
            goal=d["goal"],
            roadmap=d.get("roadmap", []),
            tasks=[_task_from_dict(t) for t in d.get("tasks", [])],
            risks=d.get("risks", []),
            questions_for_owner=d.get("questions_for_owner", []),
            created=d.get("created", time.time()),
        )

    def unmet_dependencies(self, task: Task) -> list[str]:
        """Ids this task still waits for. Ids that are not in the plan (task deleted in the
        dashboard, hand-edited plan.json) are ignored instead of blocking the task forever."""
        status = {t.id: t.status for t in self.tasks}
        return [d for d in task.depends_on if d in status and d != task.id and status[d] != "done"]

    def next_task(self) -> Task | None:
        # "in_progress" on disk with nobody running it is a leftover from a killed run
        # (Ctrl+C, crash, closed terminal); it must be runnable again, not stuck forever.
        for t in self.tasks:
            if t.status in ("todo", "in_progress") and not self.unmet_dependencies(t):
                return t
        return None

    def warnings(self) -> list[str]:
        out = []
        for t in self.tasks:
            if vague_done_when(t.done_when):
                out.append(
                    f"{t.id}: 'done when' is not checkable ({t.done_when!r}) — "
                    "edit it before running"
                )
            if len(t.description or "") < 40:
                out.append(
                    f"{t.id}: description is very short; the engineer may not know what to build"
                )
        return out

    def get(self, task_id: str) -> Task:
        for t in self.tasks:
            if t.id == task_id:
                return t
        raise KeyError(task_id)


@dataclass
class TaskOutcome:
    task: Task
    accepted: bool
    rounds: int
    verification: dict | None
    critic: dict | None
    security: dict | None
    lead_decision: dict | None
    docs: dict | None
    escalated: bool
    cost_usd: float


Reporter = Callable[[str, str], None]  # (level, message)


class Guild:
    def __init__(
        self,
        root: Path,
        cfg: ProjectConfig,
        profile: Profile,
        report: Reporter | None = None,
        *,
        dry_run: bool = False,
        on_token: Callable[[str, str], None] | None = None,
    ):
        self.root = root
        self.cfg = cfg
        self.profile = profile
        self.report = report or (lambda lvl, msg: None)
        self.dry_run = dry_run
        self.on_token = on_token  # (role, chunk) — live streaming to a UI
        self.run_id = new_run_id()
        self.run_dir = root / GUILD_DIR / "runs" / self.run_id
        self.trace = Trace(self.run_dir / "trace.jsonl", self.run_id)
        self.tracker = CostTracker()
        self.tracker.prices.update(load_prices(root / GUILD_DIR / "prices.yaml"))
        self.router = Router(
            profile, self.tracker, on_fallback=lambda slot, m, why: self._on_fallback(slot, m, why)
        )
        self.sandbox = make_sandbox(root, cfg.sandbox, cfg.docker_image)
        # set by cancel(): checked before every model call, between tool calls, while a
        # command runs and while waiting for an approval
        self.cancel_event = threading.Event()
        self.router.cancel = self.cancel_event
        self.sandbox.cancel = self.cancel_event
        self._roles: dict[str, Role] = {}
        self.trace.emit(
            "run_start", profile=profile.name, project=str(root), config=cfg.model_dump()
        )

    # ------------------------------------------------------------------ infra
    def _on_fallback(self, slot: str, model: str, why: str) -> None:
        self.trace.emit("fallback", slot=slot, model=model, reason=why)
        self.report("warn", f"{model} failed ({why}); trying next candidate for '{slot}'")

    def role(self, name: str) -> Role:
        if name not in self._roles:
            self._roles[name] = load_role(name, self.root)
        return self._roles[name]

    def _ctx(self, role: Role) -> ToolContext:
        mutating = {"write_file", "edit_file", "run_command"}
        if role.readonly is not None:
            readonly = role.readonly
        else:
            readonly = not any(t in mutating for t in role.tools) or role.name in READONLY_ROLES
        ctx = ToolContext(
            root=self.root,
            ignore=self.cfg.ignore,
            test_command=self.cfg.test_command,
            lint_command=self.cfg.lint_command,
            sandbox=self.sandbox,
            write_allowlist=role.write_allowlist,
            readonly=readonly,
        )
        # files created/edited earlier in the same task don't need re-reading before overwrite
        ctx.files_read = set(getattr(self, "_task_written", ()))
        return ctx

    def agent(self, name: str, *, escalate: bool = False) -> Agent:
        role = self.role(name)
        return Agent(
            role,
            self.router,
            self._ctx(role),
            self.trace,
            escalate=escalate,
            on_token=self.on_token,
            # the profile's per-task tool budget governs the implementation loop
            max_tool_calls=(
                self.profile.limits.max_tool_calls_per_task if name == "engineer" else None
            ),
        )

    def _phase(self, name: str, **info: Any) -> None:
        self.trace.emit("phase", phase=name, **info)
        self.report("phase", name)

    def cancel(self) -> None:
        """Ask the running job to stop as soon as it can. Raises Cancelled in the job."""
        self.cancel_event.set()

    def close(self) -> None:
        self.trace.emit(
            "run_end",
            total_usd=round(self.tracker.total_usd, 6),
            usage=self.tracker.total_usage,
            by_role={k: [v[0], v[1], v[2]] for k, v in self.tracker.by_role().items()},
        )
        self.trace.close()

    # ------------------------------------------------------------------ project context
    # Project documents that, when present, are handed to every role as context
    # (PRD/architecture/rules/tasks/memory/decisions — the "structured vibe coding" set).
    PROJECT_DOCS = (
        ("PRD", ("docs/PRD.md", "PRD.md")),
        ("Architecture", ("docs/ARCHITECTURE.md", "ARCHITECTURE.md")),
        ("Rules", ("RULES.md", "docs/RULES.md", "CLAUDE.md", ".cursorrules")),
        ("Tasks", ("TASKS.md", "docs/TASKS.md")),
        ("Memory", ("docs/MEMORY.md", "MEMORY.md")),
        ("Decisions", ("docs/DECISIONS.md", "DECISIONS.md")),
        ("Design", ("docs/DESIGN.md", "DESIGN.md")),
    )

    def project_docs(self, per_doc_chars: int = 3000) -> dict[str, str]:
        """Return {label: content} for the project docs that exist (first match per label)."""
        out: dict[str, str] = {}
        for label, candidates in self.PROJECT_DOCS:
            for rel in candidates:
                p = self.root / rel
                if p.is_file():
                    txt = p.read_text(encoding="utf-8", errors="replace").strip()
                    if txt:
                        out[f"{label} ({rel})"] = txt[:per_doc_chars] + (
                            "\n…(truncated)" if len(txt) > per_doc_chars else ""
                        )
                    break
        return out

    def project_summary(self, max_files: int = 200) -> str:
        from .tools.registry import list_files

        ctx = self._ctx(self.role("lead"))
        listing = list_files(ctx, ".", max=max_files)
        readme = ""
        for name in ("README.md", "README.rst", "README"):
            p = self.root / name
            if p.exists():
                readme = p.read_text(encoding="utf-8", errors="replace")[:4000]
                break
        parts = [f"Files:\n{listing}", f"README (truncated):\n{readme or '(none)'}"]
        for label, txt in self.project_docs().items():
            parts.append(f"{label}:\n{txt}")
        return "\n\n".join(parts)

    # ------------------------------------------------------------------ plan
    def plan(self, goal: str, extra_context: str = "") -> Plan:
        self._phase("plan", goal=goal)
        res = self.agent("lead").run(
            f"PLAN the following goal for this project.\n\nGOAL:\n{goal}\n\n"
            f"{extra_context}".strip(),
            context_blocks={"Project": self.project_summary()},
            job="plan",
        )
        if not res.ok:
            raise RuntimeError(f"Lead did not return a plan. Raw reply:\n{res.raw[:2000]}")
        d = res.data
        tasks = [
            Task(
                id=t.get("id", f"T{i + 1}"),
                title=t.get("title", ""),
                description=t.get("description", ""),
                files=t.get("files", []),
                done_when=t.get("done_when", ""),
                depends_on=t.get("depends_on", []),
            )
            for i, t in enumerate(d.get("tasks", []))
        ]
        plan = Plan(
            goal=goal,
            roadmap=d.get("roadmap", []),
            tasks=tasks,
            risks=d.get("risks", []),
            questions_for_owner=d.get("questions_for_owner", []),
        )
        self.save_plan(plan)
        self.trace.emit(
            "decision", role="lead", decision="plan", n_tasks=len(tasks), roadmap=plan.roadmap
        )
        return plan

    def plan_path(self) -> Path:
        return self.root / GUILD_DIR / "plan.json"

    def save_plan(self, plan: Plan) -> None:
        path = self.plan_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(plan.to_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, path)  # atomic: readers never see a half-written file

    def load_plan(self) -> Plan | None:
        p = self.plan_path()
        if not p.exists():
            return None
        return Plan.from_dict(json.loads(p.read_text(encoding="utf-8")))

    # ------------------------------------------------------------------ git helpers
    def _git_rc(self, *args: str) -> tuple[int, str]:
        """(exit code, stdout+stderr). Use this when an error message must not be mistaken
        for data (a diff, a branch name)."""
        try:
            r = subprocess.run(
                ["git", *args],
                cwd=self.root,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as e:  # git not installed
            return 127, f"git error: {e}"
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()

    def _git(self, *args: str, check: bool = False) -> str:
        rc, out = self._git_rc(*args)
        if check and rc != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {out}")
        return out

    def _untracked(self) -> set[str]:
        # -z: plain names; without it git quotes non-ASCII paths and they never match again
        return set(
            filter(None, self._git("ls-files", "-z", "--others", "--exclude-standard").split("\0"))
        )

    def _has_git(self) -> bool:
        return (self.root / ".git").is_dir()

    def _start_branch(self, task: Task) -> str | None:
        """Create guild/<task> from the current HEAD. Only *modified tracked* files count as
        dirty; pre-existing untracked files are tolerated and kept out of guild's commits."""
        self._on_branch = False
        self._pre_untracked = set()
        self._base_branch = None
        if not self._has_git() or self.dry_run:
            return None
        # captured even when no branch is made: the review diff uses it to show only the files
        # this task created
        self._pre_untracked = self._untracked()
        if not self._git("rev-parse", "--verify", "--quiet", "HEAD").startswith(
            tuple("0123456789abcdef")
        ):
            self.report(
                "warn",
                "git repository has no commits yet; guild will not create a branch and will "
                'NOT commit. Run: git add -A && git commit -m "initial commit"',
            )
            return None
        branch = f"guild/{_slug(task.id, 30) or 'task'}-{_slug(task.title, 40)}".rstrip("-")
        if self._git(
            "status",
            "--porcelain",
            "--untracked-files=no",
            "--",
            ".",
            f":(exclude){GUILD_DIR}",
            ":(exclude).gitignore",
        ):
            self.report(
                "warn",
                "you have uncommitted changes to tracked files; guild will not create a "
                "branch and will NOT commit. Commit or stash first for a clean branch.",
            )
            return None
        # Remember where we started. An accepted task stays on its branch so dependent tasks
        # build on it (run all); a rejected task goes back here so its broken work never leaks
        # into the next task's branch.
        current = self.current_branch()
        self._base_branch = current if current and current != "HEAD" else None
        self._git("checkout", "-B", branch, check=True)
        self._on_branch = True
        return branch

    def _leave_branch(self) -> None:
        """After a task that was NOT accepted (or crashed): go back to the branch the task
        started from, so the next task does not build on broken work. The attempt stays on its
        guild/ branch for inspection; the dashboard can still diff/discard it."""
        base = getattr(self, "_base_branch", None)
        if not base or not getattr(self, "_on_branch", False) or not self._has_git():
            return
        if self._git("status", "--porcelain", "--untracked-files=no"):
            return  # never discard uncommitted work; stay where we are
        self._git("checkout", "-q", base)
        self._on_branch = False

    def _commit(self, message: str) -> None:
        # Never commit unless we are on a branch guild created — protects main/master.
        if not self._has_git() or self.dry_run or not getattr(self, "_on_branch", False):
            return
        self._git("add", "-A", "--", ".", f":(exclude){GUILD_DIR}", *_COMMIT_EXCLUDES)
        for f in getattr(self, "_pre_untracked", ()):
            self._git("reset", "-q", "--", f":(literal){f}")
        if self._git_rc("diff", "--cached", "--quiet")[0] == 0:
            return  # nothing staged
        rc, out = self._git_rc("commit", "-q", "-m", message)
        if rc != 0 and re.search(r"tell me who you are|auto-detect email|empty ident", out):
            # fresh git install without user.name/user.email: commit as "guild" rather than
            # silently leaving an accepted task uncommitted
            rc, out = self._git_rc(
                "-c",
                "user.name=guild",
                "-c",
                "user.email=guild@localhost",
                "commit",
                "-q",
                "-m",
                message,
            )
            if rc == 0:
                self.report(
                    "warn",
                    "git has no user.name/user.email; committed as 'guild'. Set them with: "
                    'git config --global user.name "You" && '
                    'git config --global user.email "you@example.com"',
                )
        if rc != 0:
            self.trace.emit("note", role="guild", msg=f"git commit failed: {out[:300]}")
            self.report("warn", f"git commit failed — the work is NOT on the branch: {out[:200]}")

    _NEW_FILE_CHARS = 20_000

    def _new_files_diff(self) -> str:
        """Untracked files created since the task started, as a diff. Plain `git diff` never
        shows them, so without a branch (dirty tree, no commits yet) or after a failed commit
        the reviewers would not see the new code at all."""
        chunks: list[str] = []
        skip = ("__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", GUILD_DIR, ".git")
        for rel in sorted(self._untracked() - set(getattr(self, "_pre_untracked", ()))):
            if rel.endswith(".pyc") or any(part in skip for part in Path(rel).parts):
                continue
            p = self.root / rel
            try:
                raw = p.read_bytes()[: self._NEW_FILE_CHARS]
            except OSError:
                continue
            head = f"diff --git a/{rel} b/{rel}\nnew file\n--- /dev/null\n+++ b/{rel}\n"
            if b"\x00" in raw:
                chunks.append(head + "(binary file)")
                continue
            body = raw.decode("utf-8", errors="replace")
            chunks.append(head + "\n".join("+" + ln for ln in body.splitlines()))
        return "\n".join(chunks)

    def _diff_text(self, base: str | None) -> str:
        if not self._has_git():
            return "(no git; review working tree)"
        excl = ["--", ".", f":(exclude){GUILD_DIR}"]

        def diff(*args: str) -> str:
            rc, out = self._git_rc("diff", "--no-ext-diff", *args, *excl)
            return out if rc == 0 else ""  # an error message is not a diff

        tracked = diff(base or "HEAD~1") or diff() or diff("--cached")
        text = "\n".join(x for x in (tracked, self._new_files_diff()) if x)
        return text or "(no diff)"

    # ------------------------------------------------------ branch helpers (for the UI / human)
    def default_branch(self) -> str:
        for cand in ("main", "master"):
            if self._branch_exists(cand):
                return cand
        cur = self.current_branch()
        return cur if cur != "HEAD" else "main"

    def current_branch(self) -> str:
        """Branch name, or "HEAD" when detached. Works on a repository with no commits."""
        rc, out = self._git_rc("symbolic-ref", "--short", "-q", "HEAD")
        return out if rc == 0 and out else "HEAD"

    def _branch_exists(self, name: str | None) -> bool:
        return (
            bool(name)
            and not name.startswith("-")
            and (self._git_rc("rev-parse", "--verify", "--quiet", f"refs/heads/{name}")[0] == 0)
        )

    def branch_diff(self, branch: str, base: str | None = None) -> dict:
        base = base if self._branch_exists(base) else self.default_branch()
        if not self._branch_exists(branch):
            return {"branch": branch, "base": base, "exists": False, "diff": "", "stat": ""}
        rng = f"{base}...{branch}"
        return {
            "branch": branch,
            "base": base,
            "exists": True,
            "stat": self._git("diff", "--stat", rng, "--", ".", f":(exclude){GUILD_DIR}"),
            "diff": self._git("diff", rng, "--", ".", f":(exclude){GUILD_DIR}"),
            "commits": self._git("log", "--oneline", f"{base}..{branch}"),
        }

    def merge_branch(self, branch: str, base: str | None = None) -> dict:
        """Merge a guild branch into the base branch. Refuses on a dirty tree or conflicts.

        base: the branch the task started from (Task.base). When it is unknown or gone (e.g. a
        stacked guild branch that was merged and deleted) the default branch is used."""
        base = base if self._branch_exists(base) else self.default_branch()
        if base == branch:
            return {"ok": False, "error": f"cannot merge {branch} into itself (no base branch)"}
        if not self._branch_exists(branch):
            return {"ok": False, "error": f"branch {branch} does not exist"}
        if self._git(
            "status",
            "--porcelain",
            "--untracked-files=no",
            "--",
            ".",
            f":(exclude){GUILD_DIR}",
            ":(exclude).gitignore",
        ):
            return {"ok": False, "error": "working tree is dirty; commit or stash first"}
        rc, out = self._git_rc("checkout", "-q", base)
        if rc != 0:
            return {"ok": False, "error": f"could not switch to {base}:\n{out}"}
        rc, out = self._git_rc("merge", "--no-ff", "-q", "-m", f"Merge {branch} (guild)", branch)
        if "CONFLICT" in out or self._git("ls-files", "-u"):
            self._git("merge", "--abort")
            return {"ok": False, "error": f"merge conflict; resolve manually:\n{out}"}
        if rc != 0:  # exit code, not the words "error"/"fatal" (a branch may be named that)
            return {"ok": False, "error": out or f"git merge exited {rc}"}
        deleted = self._git("branch", "-d", branch)
        return {
            "ok": True,
            "base": base,
            "merged": branch,
            "output": (out + "\n" + deleted).strip(),
        }

    def discard_branch(self, branch: str) -> dict:
        # `git branch -D` is unrecoverable for unmerged work: only ever on guild's own branches
        if not str(branch).startswith("guild/"):
            return {"ok": False, "output": f"refusing to delete {branch}: not a guild/ branch"}
        if self.current_branch() == branch:
            rc, out = self._git_rc("checkout", "-q", self.default_branch())
            if rc != 0:
                return {"ok": False, "output": f"could not leave {branch}:\n{out}"}
        rc, out = self._git_rc("branch", "-D", "--", branch)
        return {"ok": rc == 0, "output": out}

    # ------------------------------------------------------------------ verification
    _PYTEST_RX = re.compile(r"(\d+) passed|(\d+) failed|(\d+) error", re.I)
    _FAIL_LINE_RX = re.compile(r"^(FAILED|ERROR) (.+?)(?: - (.*))?$", re.M)

    def collect_tests(self) -> set[str] | None:
        """Set of pytest node ids (file::test). None when the test runner isn't pytest."""
        cmd = self.cfg.test_command
        if "pytest" not in cmd:
            return None
        # exactly one -q: with two, pytest prints per-file counts instead of node ids
        base = " ".join(t for t in cmd.split() if t not in ("-q", "--quiet", "-qq"))
        res = self.sandbox.run(f"{base} --collect-only -q", timeout=300)
        ids = {
            ln.strip()
            for ln in res.output.splitlines()
            if "::" in ln and not ln.startswith(("=", "ERROR", "E "))
        }
        return ids

    def verify(self) -> dict:
        """Run the project's test command directly (no model involved) and report facts."""
        cmd = self.cfg.test_command
        res = self.sandbox.run(cmd, timeout=600)
        out = res.output
        passed_n = failed_n = 0
        for m in self._PYTEST_RX.finditer(out):
            if m.group(1):
                passed_n += int(m.group(1))
            if m.group(2):
                failed_n += int(m.group(2))
            if m.group(3):
                failed_n += int(m.group(3))
        failures = [
            f"{m.group(2)}: {m.group(3) or ''}".strip(": ")
            for m in self._FAIL_LINE_RX.finditer(out)
        ][:20]
        no_tests = res.returncode == 5 and "pytest" in cmd  # pytest: "no tests collected"
        if no_tests:
            res.returncode = 0
            tail_note = "(no tests collected)"
        else:
            tail_note = ""
        if res.returncode != 0 and not failures:
            failures = [f"{cmd} exited {res.returncode}"]
        tail = "\n".join(out.strip().splitlines()[-25:]) + ("\n" + tail_note if tail_note else "")
        result = {
            "passed": res.returncode == 0,
            "tests_run": passed_n + failed_n,
            "failures": failures,
            "output_tail": tail,
            "command": cmd,
            "exit_code": res.returncode,
        }
        if self.cfg.lint_command and res.returncode == 0:
            lint = self.sandbox.run(self.cfg.lint_command, timeout=300)
            result["lint_passed"] = lint.returncode == 0
            if lint.returncode != 0:
                result["passed"] = False
                result["failures"].append(f"lint: {self.cfg.lint_command} exited {lint.returncode}")
                result["output_tail"] += "\n--- lint ---\n" + "\n".join(
                    lint.output.strip().splitlines()[-15:]
                )
        return result

    # ------------------------------------------------------------------ run one task
    def run_task(self, plan: Plan, task: Task, *, roles: list[str] | None = None) -> TaskOutcome:
        try:
            return self._run_task(plan, task, roles=roles)
        except BaseException as e:  # incl. KeyboardInterrupt: Ctrl+C must not strand the task
            import traceback

            interrupted = isinstance(e, (KeyboardInterrupt, SystemExit, Cancelled))
            self.trace.emit(
                "error",
                task_id=task.id,
                error="interrupted" if interrupted else f"{type(e).__name__}: {e}",
                traceback="" if interrupted else traceback.format_exc()[-4000:],
            )
            if task.status == "in_progress":
                task.status = "todo"
                task.notes = (
                    "interrupted before it finished"
                    if interrupted
                    else f"guild error: {type(e).__name__}: {e}"
                )
                self.save_plan(plan)
            self._leave_branch()
            raise

    def _run_task(self, plan: Plan, task: Task, *, roles: list[str] | None = None) -> TaskOutcome:
        enabled = roles or self.cfg.roles_enabled
        limits = self.profile.limits
        self._phase("task_start", task_id=task.id, title=task.title)
        task.status = "in_progress"
        self.save_plan(plan)

        task.branch = self._start_branch(task)
        if task.branch:
            # Where to merge back to. A task stacked on another guild branch (run all) inherits
            # that task's base, so the dashboard never merges it into a throwaway branch.
            base = self._base_branch
            if base and base.startswith("guild/"):
                under = next((t for t in plan.tasks if t.branch == base and t is not task), None)
                base = under.base if under else None
            task.base = base
        base_ref = (
            self._git("rev-parse", "--verify", "--quiet", "HEAD") if self._has_git() else None
        )
        if base_ref and not re.fullmatch(r"[0-9a-f]{40}", base_ref):
            base_ref = None  # no commits yet: diff the working tree instead
        cost0 = self.tracker.total_usd

        missing = [f for f in task.files if not (self.root / f).exists()]
        task_brief = (
            f"TASK {task.id}: {task.title}\n\n{task.description}\n\n"
            f"Files likely involved: {', '.join(task.files) or 'unknown'}\n"
            + (
                "NOTE: these files do not exist yet — CREATE them with write_file: "
                f"{', '.join(missing)}\n"
                if missing
                else ""
            )
            + f"Done when: {task.done_when}"
        )
        project_ctx = {"Project goal": plan.goal, "Project": self.project_summary()}

        self._task_written: set[str] = set()
        # Baseline: which tests exist before the engineer touches anything. Tests that vanish
        # afterwards mean existing behaviour was deleted, whatever the new tests say.
        baseline_tests = self.collect_tests() if "verifier" in enabled else None
        baseline_count = None
        if "verifier" in enabled and baseline_tests is None:
            baseline_count = self.verify().get("tests_run")

        feedback = ""
        verification = critic = security = lead_decision = docs = None
        escalated = False
        failed_rounds = 0
        accepted = False
        rounds = 0
        pushed_back_on_block = False

        for rounds in range(1, limits.max_revision_rounds + 1):
            self._phase("implement", task_id=task.id, round=rounds, escalated=escalated)
            eng_agent = self.agent("engineer", escalate=escalated)
            eng = eng_agent.run(
                task_brief + (f"\n\nREVIEW FEEDBACK TO ADDRESS:\n{feedback}" if feedback else ""),
                context_blocks=project_ctx,
            )
            if eng.data and eng.data.get("status") == "done" and not eng_agent.ctx.files_written:
                # Common small-model failure: code written as prose in the reply, nothing on disk.
                self.trace.emit(
                    "note", role="engineer", msg="reported done but changed no files; asking again"
                )
                self.report(
                    "warn",
                    "engineer reported done but changed no files — asking it to use the tools",
                )
                eng = eng_agent.run(
                    task_brief
                    + "\n\nIMPORTANT: your previous reply described code but did not change any "
                    "file. Code written in the reply is DISCARDED. The ONLY way to change the "
                    "project is the write_file / edit_file tools. Call them now with the full file "
                    "contents, run run_tests, then reply with the final JSON."
                    + (f"\n\nREVIEW FEEDBACK TO ADDRESS:\n{feedback}" if feedback else ""),
                    context_blocks=project_ctx,
                )
                if (
                    eng.data
                    and eng.data.get("status") == "done"
                    and not eng_agent.ctx.files_written
                ):
                    eng.data["status"] = "blocked"
                    eng.data["summary"] = (
                        "engineer produced no file changes after two attempts "
                        "(model wrote code as text)"
                    )
            self.trace.emit(
                "decision",
                role="engineer",
                round=rounds,
                status=(eng.data or {}).get("status"),
                summary=(eng.data or {}).get("summary", eng.raw[:300]),
            )
            self._commit(f"guild({task.id}) round {rounds}: {task.title}")

            self._task_written |= eng_agent.ctx.files_written
            if eng.data and eng.data.get("status") == "blocked" and not pushed_back_on_block:
                # Small models block on trivial obstacles (a missing file, a failing test). Push
                # back once with the facts; only a second "blocked" is taken at face value.
                pushed_back_on_block = True
                why = eng.data.get("summary", "")
                self.trace.emit(
                    "note", role="engineer", msg=f"blocked once ({why[:120]}); pushing back"
                )
                self.report("warn", f"engineer says blocked ({why[:80]}…) — pushing back once")
                eng = eng_agent.run(
                    task_brief
                    + "\n\nYou reported BLOCKED because: "
                    + why
                    + "\n\nThat is not a valid reason. Facts: a file that does not exist must "
                    "be CREATED with write_file (this is normal for new projects). A failing or "
                    "missing test must be fixed or written by you. You have all the tools you "
                    "need. Implement the task now: create or edit the files, run run_tests, then "
                    'reply with the final JSON. Use "blocked" only if a tool call literally fails '
                    "and you can quote its error.",
                    context_blocks=project_ctx,
                )
                self._task_written |= eng_agent.ctx.files_written
                # the round's commit above ran before this second attempt; without committing
                # again its files stay untracked: absent from the branch and the review diff
                self._commit(f"guild({task.id}) round {rounds}: {task.title}")
                if (
                    eng.data
                    and eng.data.get("status") == "done"
                    and not eng_agent.ctx.files_written
                ):
                    eng.data["status"] = "blocked"
                    eng.data["summary"] = "engineer produced no file changes (after push-back)"
            if eng.data and eng.data.get("status") == "blocked":
                task.status = "blocked"
                task.notes = eng.data.get("summary", "")
                self.report("warn", f"engineer blocked: {task.notes}")
                break

            # verify — deterministic: guild runs the tests itself, no model in the loop
            verification = None
            if "verifier" in enabled:
                self._phase("verify", task_id=task.id, round=rounds)
                verification = self.verify()
                removed: list[str] = []
                if baseline_tests:
                    now = self.collect_tests() or set()
                    removed = sorted(baseline_tests - now)
                elif (
                    baseline_count is not None and verification.get("tests_run", 0) < baseline_count
                ):
                    now = verification.get("tests_run")
                    removed = [f"test count dropped from {baseline_count} to {now}"]
                if removed:
                    verification["passed"] = False
                    verification["removed_tests"] = removed
                    verification["failures"] = [
                        *verification.get("failures", []),
                        "EXISTING TESTS REMOVED (you must keep all pre-existing code and tests): "
                        + ", ".join(removed[:8]),
                    ]
                self.trace.emit(
                    "verification",
                    round=rounds,
                    **{
                        k: verification.get(k)
                        for k in ("passed", "tests_run", "failures", "exit_code", "removed_tests")
                    },
                )
                v_status = "PASS" if verification.get("passed") else "FAIL"
                self.report(
                    "info", f"verification: {v_status} ({verification.get('tests_run')} tests)"
                )

            diff = self._diff_text(base_ref)
            self.trace.emit(
                "review_input",
                round=rounds,
                diff_chars=len(diff),
                files_in_diff=_files_in_diff(diff),
            )
            review_ctx = {
                "Task": task_brief,
                "Diff": diff[:30000],
                "Verification": json.dumps(verification) if verification else "not run",
            }

            # review
            critic = security = None
            if "critic" in enabled:
                self._phase("critique", task_id=task.id, round=rounds)
                critic = (
                    self.agent("critic").run("Review the change.", context_blocks=review_ctx)
                ).data
            if "security" in enabled:
                self._phase("security", task_id=task.id, round=rounds)
                security = (
                    self.agent("security").run(
                        "Review the change for security and privacy.", context_blocks=review_ctx
                    )
                ).data

            for label, rev in (("critic", critic), ("security", security)):
                if label in enabled and rev is None:
                    # parse + repairs failed: never treat silence as approval
                    self.report(
                        "warn", f"{label} returned no valid JSON; treating as changes requested"
                    )
                    self.trace.emit("note", role=label, msg="no valid JSON reply; not an approval")
                    placeholder = {
                        "verdict": "request_changes",
                        "summary": f"{label} did not produce a valid review (no JSON reply)",
                        "findings": [],
                    }
                    if label == "critic":
                        critic = placeholder
                    else:
                        security = placeholder

            passed = (verification is None) or bool(verification.get("passed"))
            critic_ok = critic is None or critic.get("verdict") == "approve"
            sec_ok = security is None or security.get("verdict") == "approve"
            sec_block = security is not None and security.get("verdict") == "block"

            if passed and critic_ok and sec_ok:
                accepted = True
                break

            failed_rounds += 1  # noqa: SIM113 — counts failed rounds, not loop iterations
            if (
                limits.escalate_after
                and failed_rounds >= limits.escalate_after
                and not escalated
                and self.role("engineer").escalation_slot in self.profile.slots
            ):
                escalated = True
                self.report("warn", "escalating engineer to stronger model")

            feedback = _compose_feedback(verification, critic, security)
            self.report(
                "info",
                f"round {rounds}: changes requested" + (" (security BLOCK)" if sec_block else ""),
            )

        # lead decision
        if accepted and "lead" in enabled:
            self._phase("lead_review", task_id=task.id)
            res = self.agent("lead").run(
                "REVIEW this task's outcome and decide.",
                context_blocks={
                    "Task": task_brief,
                    "Diff": self._diff_text(base_ref)[:30000],
                    "Verification": json.dumps(verification),
                    "Critic": json.dumps(critic),
                    "Security": json.dumps(security),
                },
                job="review",
            )
            lead_decision = res.data or {"decision": "REVISE", "notes": "lead returned no JSON"}
            self.trace.emit(
                "decision",
                role="lead",
                decision=lead_decision.get("decision"),
                notes=lead_decision.get("notes", ""),
            )
            accepted = lead_decision.get("decision") == "ACCEPT"

        if accepted:
            task.status = "done"
            if "docs" in enabled:
                self._phase("docs", task_id=task.id)
                docs = (
                    self.agent("docs").run(
                        "Update documentation for the accepted change.",
                        context_blocks={
                            "Task": task_brief,
                            "Diff": self._diff_text(base_ref)[:20000],
                        },
                    )
                ).data
                self._commit(f"guild({task.id}) docs")
        elif task.status != "blocked":
            task.status = "todo"
            notes = feedback
            if lead_decision and lead_decision.get("decision") != "ACCEPT":
                # the Lead's reason is the only explanation the owner gets for a rejection
                notes = "\n".join(
                    [
                        f"LEAD {lead_decision.get('decision')}: {lead_decision.get('notes', '')}",
                        *(f"- {i}" for i in lead_decision.get("improvements") or []),
                    ]
                )
            task.notes = notes[:2000]

        self.save_plan(plan)
        if not accepted:
            self._leave_branch()
        outcome = TaskOutcome(
            task=task,
            accepted=accepted,
            rounds=rounds,
            verification=verification,
            critic=critic,
            security=security,
            lead_decision=lead_decision,
            docs=docs,
            escalated=escalated,
            cost_usd=self.tracker.total_usd - cost0,
        )
        self.trace.emit(
            "task_end",
            task_id=task.id,
            accepted=accepted,
            rounds=rounds,
            escalated=escalated,
            cost_usd=round(outcome.cost_usd, 6),
            branch=task.branch,
        )
        return outcome

    # ------------------------------------------------------------------ run several
    def run_tasks(
        self,
        plan: Plan,
        task_ids: list[str] | None,
        *,
        all_tasks: bool = False,
        roles: list[str] | None = None,
        stop_on_failure: bool = True,
    ) -> list[TaskOutcome]:
        """Run specific task ids in order, or every runnable task when all_tasks is set."""
        outcomes: list[TaskOutcome] = []
        if task_ids:
            # explicitly selected tasks all get their turn; a failure only skips dependants
            failed: set[str] = set()
            known = {t.id for t in plan.tasks}
            unknown = [tid for tid in task_ids if tid not in known]
            if unknown:
                raise RuntimeError(
                    f"no task {', '.join(unknown)} in the plan (tasks: {', '.join(sorted(known))})"
                )
            for tid in task_ids:
                task = plan.get(tid)
                if task.status == "done":
                    continue
                not_done = sorted(
                    set(plan.unmet_dependencies(task)) | (set(task.depends_on) & failed)
                )
                if not_done:
                    self.report(
                        "warn",
                        f"skipping {tid}: depends on {', '.join(not_done)} which is not done yet",
                    )
                    continue
                out = self.run_task(plan, task, roles=roles)
                outcomes.append(out)
                if not out.accepted:
                    failed.add(tid)
            return outcomes
        while True:
            task = plan.next_task()
            if task is None:
                break
            out = self.run_task(plan, task, roles=roles)
            outcomes.append(out)
            if not all_tasks or (stop_on_failure and not out.accepted):
                break
        return outcomes

    # ------------------------------------------------------------------ chat
    def chat(self, question: str, history: list[dict] | None = None) -> AgentResult:
        """Conversational Q&A about the project.

        history: [{"role": "user" | "assistant", "content": str}]
        """
        from .providers import Message as _M

        self._phase("chat")
        plan = self.load_plan()
        blocks = {"Project": self.project_summary()}
        if plan:
            blocks["Current plan"] = json.dumps(plan.to_dict(), indent=1)[:6000]
        prior = [
            _M(h["role"], str(h["content"]))
            for h in (history or [])[-12:]
            if isinstance(h, dict) and h.get("role") in ("user", "assistant") and h.get("content")
        ]
        return self.agent("assistant").run(
            question, context_blocks=blocks, prior=prior, expect_json=False
        )

    # ------------------------------------------------------------------ pair programming
    def pair(
        self,
        message: str,
        history: list[dict] | None = None,
        *,
        mode: str = "ask",
        approver: Callable[[dict], tuple[bool, str]] | None = None,
    ) -> AgentResult:
        """One turn of interactive pair programming in the owner's working tree.

        Unlike run_task there is no branch, no commit and no review round: the owner is the
        reviewer, live. `approver(request) -> (approved, reason)` is asked before every file
        change and every non-inspection command (see PAIR_MODES). With no approver nothing
        that needs approval happens: the gate fails closed.

        history: [{"role": "user" | "assistant", "content": str}]
        """
        from .providers import Message as _M

        if mode not in PAIR_MODES:
            raise ValueError(f"mode must be one of {PAIR_MODES}, not {mode!r}")
        self._phase("pair", mode=mode)
        agent = self.agent("pair")
        ctx = agent.ctx
        if mode == "plan":
            ctx.readonly = True
        else:

            def gate(req: dict) -> tuple[bool, str]:
                auto = mode == "auto-edits" and req.get("kind") == "edit"
                if auto:
                    approved, why = True, "auto-edits"
                elif approver is None:
                    approved, why = False, "nobody is available to approve it"
                else:
                    approved, why = approver(req)
                self.trace.emit(
                    "approval",
                    tool=req.get("tool"),
                    target=req.get("path") or req.get("command"),
                    approved=bool(approved),
                    auto=auto,
                    reason=why,
                )
                return bool(approved), why

            ctx.approver = gate
        blocks = {
            "Project": self.project_summary(),
            "Mode": {
                "ask": "ASK: the owner approves each file change and command.",
                "auto-edits": "AUTO-EDITS: file changes are applied directly; commands need "
                "the owner's approval.",
                "plan": "PLAN: read-only. Explore, then reply with a step-by-step plan. "
                "Do not call write_file, edit_file or commands that change anything.",
            }[mode],
        }
        prior = [
            _M(h["role"], str(h["content"]))
            for h in (history or [])[-16:]
            if isinstance(h, dict) and h.get("role") in ("user", "assistant") and h.get("content")
        ]
        res = agent.run(message, context_blocks=blocks, prior=prior, expect_json=False)
        res.data = {"text": res.raw, "files_changed": sorted(ctx.files_written), "mode": mode}
        self.trace.emit(
            "decision",
            role="pair",
            decision="reply",
            summary=res.raw[:300],
            files=res.data["files_changed"],
        )
        return res

    # ------------------------------------------------------------------ plan revision
    def revise_plan(self, instruction: str) -> Plan:
        """Ask the Lead to rewrite the current plan according to a natural-language instruction."""
        current = self.load_plan()
        if current is None:
            return self.plan(instruction)
        self._phase("revise_plan", instruction=instruction)
        res = self.agent("lead").run(
            "REVISE the current plan according to the owner's instruction. Keep task ids of tasks "
            "that stay unchanged; keep status/branch of tasks already done. Reply with the FULL "
            "updated plan in the PLAN JSON format.\n\nOWNER'S INSTRUCTION:\n" + instruction,
            context_blocks={
                "Project goal": current.goal,
                "Current plan (JSON)": json.dumps(current.to_dict(), indent=1),
                "Project": self.project_summary(),
            },
            job="plan",
        )
        if not res.ok:
            raise RuntimeError(f"Lead did not return a plan. Raw reply:\n{res.raw[:2000]}")
        d = res.data
        old = {t.id: t for t in current.tasks}
        tasks = []
        for i, t in enumerate(d.get("tasks", [])):
            tid = t.get("id") or f"T{i + 1}"
            prev = old.get(tid)
            status = prev.status if prev and prev.status == "done" else "todo"
            tasks.append(
                Task(
                    id=tid,
                    title=t.get("title", ""),
                    description=t.get("description", ""),
                    files=t.get("files", []),
                    done_when=t.get("done_when", ""),
                    depends_on=t.get("depends_on", []),
                    status=status,
                    branch=prev.branch if prev else None,
                    notes=prev.notes if prev else "",
                    merged=bool(prev and prev.merged and prev.status == "done"),
                    base=prev.base if prev else None,
                )
            )
        plan = Plan(
            goal=current.goal,
            roadmap=d.get("roadmap", current.roadmap),
            tasks=tasks,
            risks=d.get("risks", []),
            questions_for_owner=d.get("questions_for_owner", []),
            created=current.created,
        )
        self.save_plan(plan)
        self.trace.emit("decision", role="lead", decision="revise_plan", n_tasks=len(tasks))
        return plan

    # ------------------------------------------------------------------ one-off ask
    def ask(self, role_name: str, question: str) -> AgentResult:
        self._phase("ask", role=role_name)
        return self.agent(role_name).run(
            question, context_blocks={"Project": self.project_summary()}
        )


def _compose_feedback(verification: dict | None, critic: dict | None, security: dict | None) -> str:
    parts: list[str] = []
    if verification and not verification.get("passed"):
        parts.append(
            "TESTS FAILED:\n"
            + "\n".join(f"- {f}" for f in verification.get("failures", []))
            + "\n\nOutput tail:\n"
            + str(verification.get("output_tail", ""))[:2000]
        )
    for label, rev in (("CRITIC", critic), ("SECURITY", security)):
        if rev and rev.get("verdict") != "approve":
            items = rev.get("findings", [])
            parts.append(
                f"{label} ({rev.get('verdict')}): {rev.get('summary', '')}\n"
                + "\n".join(
                    f"- [{f.get('severity')}] {f.get('file')}: {f.get('issue')} → "
                    f"{f.get('suggestion') or f.get('fix')}"
                    for f in items
                )
            )
    return "\n\n".join(parts) or "No specific feedback."
