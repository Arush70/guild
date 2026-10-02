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
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agent import Agent, AgentResult
from .config import GUILD_DIR, Profile, ProjectConfig, Role, load_role
from .providers import CostTracker, Router
from .sandbox import make_sandbox
from .tools import ToolContext
from .trace import Trace, new_run_id


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
            tasks=[Task(**t) for t in d.get("tasks", [])],
            risks=d.get("risks", []),
            questions_for_owner=d.get("questions_for_owner", []),
            created=d.get("created", time.time()),
        )

    def next_task(self) -> Task | None:
        done = {t.id for t in self.tasks if t.status == "done"}
        for t in self.tasks:
            if t.status == "todo" and all(d in done for d in t.depends_on):
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
        self.router = Router(
            profile, self.tracker, on_fallback=lambda slot, m, why: self._on_fallback(slot, m, why)
        )
        self.sandbox = make_sandbox(root, cfg.sandbox, cfg.docker_image)
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
        readonly = not any(t in mutating for t in role.tools) or role.name in {
            "critic",
            "security",
            "verifier",
        }
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
        )

    def _phase(self, name: str, **info: Any) -> None:
        self.trace.emit("phase", phase=name, **info)
        self.report("phase", name)

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
    def _git(self, *args: str, check: bool = False) -> str:
        r = subprocess.run(
            ["git", *args], cwd=self.root, capture_output=True, encoding="utf-8", errors="replace"
        )
        if check and r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
        return (r.stdout + r.stderr).strip()

    def _has_git(self) -> bool:
        return (self.root / ".git").is_dir()

    def _start_branch(self, task: Task) -> str | None:
        """Create guild/<task> from the current HEAD. Only *modified tracked* files count as
        dirty; pre-existing untracked files are tolerated and kept out of guild's commits."""
        self._on_branch = False
        self._pre_untracked = set()
        if not self._has_git() or self.dry_run:
            return None
        slug = "".join(c if c.isalnum() else "-" for c in task.title.lower())[:40].strip("-")
        branch = f"guild/{task.id.lower()}-{slug}"
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
        self._pre_untracked = set(
            filter(None, self._git("ls-files", "--others", "--exclude-standard").splitlines())
        )
        self._git("checkout", "-B", branch, check=True)
        self._on_branch = True
        return branch

    def _commit(self, message: str) -> None:
        # Never commit unless we are on a branch guild created — protects main/master.
        if not self._has_git() or self.dry_run or not getattr(self, "_on_branch", False):
            return
        self._git("add", "-A", "--", ".", f":(exclude){GUILD_DIR}")
        for f in getattr(self, "_pre_untracked", ()):
            self._git("reset", "-q", "--", f)
        if (
            self._git("diff", "--cached", "--quiet") == ""
            and self._git("diff", "--cached", "--stat") == ""
        ):
            return
        self._git("commit", "-q", "-m", message)

    def _diff_text(self, base: str | None) -> str:
        if not self._has_git():
            return "(no git; review working tree)"
        excl = ["--", ".", f":(exclude){GUILD_DIR}"]
        return (
            self._git("diff", base or "HEAD~1", *excl)
            or self._git("diff", *excl)
            or self._git("diff", "--cached", *excl)
            or "(no diff)"
        )

    # ------------------------------------------------------ branch helpers (for the UI / human)
    def default_branch(self) -> str:
        for cand in ("main", "master"):
            if self._git("rev-parse", "--verify", "--quiet", cand):
                return cand
        return self._git("rev-parse", "--abbrev-ref", "HEAD") or "main"

    def current_branch(self) -> str:
        return self._git("rev-parse", "--abbrev-ref", "HEAD")

    def branch_diff(self, branch: str, base: str | None = None) -> dict:
        base = base or self.default_branch()
        if not self._git("rev-parse", "--verify", "--quiet", branch):
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
        """Merge a guild branch into the base branch. Refuses on a dirty tree or conflicts."""
        base = base or self.default_branch()
        if self._git(
            "status", "--porcelain", "--", ".", f":(exclude){GUILD_DIR}", ":(exclude).gitignore"
        ):
            return {"ok": False, "error": "working tree is dirty; commit or stash first"}
        self._git("checkout", "-q", base, check=True)
        out = self._git("merge", "--no-ff", "-q", "-m", f"Merge {branch} (guild)", branch)
        if "CONFLICT" in out or self._git("ls-files", "-u"):
            self._git("merge", "--abort")
            return {"ok": False, "error": f"merge conflict; resolve manually:\n{out}"}
        self._git("branch", "-d", branch)
        return {"ok": True, "base": base, "merged": branch, "output": out}

    def discard_branch(self, branch: str) -> dict:
        base = self.default_branch()
        if self.current_branch() == branch:
            self._git("checkout", "-q", base, check=True)
        out = self._git("branch", "-D", branch)
        return {"ok": "Deleted" in out or "deleted" in out, "output": out}

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
        except Exception as e:
            import traceback

            self.trace.emit(
                "error",
                task_id=task.id,
                error=f"{type(e).__name__}: {e}",
                traceback=traceback.format_exc()[-4000:],
            )
            if task.status == "in_progress":
                task.status = "todo"
                task.notes = f"guild error: {type(e).__name__}: {e}"
                self.save_plan(plan)
            raise

    def _run_task(self, plan: Plan, task: Task, *, roles: list[str] | None = None) -> TaskOutcome:
        enabled = roles or self.cfg.roles_enabled
        limits = self.profile.limits
        self._phase("task_start", task_id=task.id, title=task.title)
        task.status = "in_progress"
        self.save_plan(plan)

        base_ref = self._git("rev-parse", "HEAD") if self._has_git() else None
        task.branch = self._start_branch(task)
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
            task.notes = feedback[:2000]

        self.save_plan(plan)
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
            for tid in task_ids:
                task = plan.get(tid)
                if task.status == "done":
                    continue
                not_done = [
                    d for d in task.depends_on if d in failed or plan.get(d).status != "done"
                ]
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
        prior = [_M(h["role"], h["content"]) for h in (history or [])[-12:] if h.get("content")]
        return self.agent("assistant").run(
            question, context_blocks=blocks, prior=prior, expect_json=False
        )

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
