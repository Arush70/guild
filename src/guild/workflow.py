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
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

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


@dataclass
class Plan:
    goal: str
    roadmap: list[str]
    tasks: list[Task]
    risks: list[str] = field(default_factory=list)
    questions_for_owner: list[str] = field(default_factory=list)
    created: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {"goal": self.goal, "roadmap": self.roadmap, "risks": self.risks,
                "questions_for_owner": self.questions_for_owner, "created": self.created,
                "tasks": [t.__dict__ for t in self.tasks]}

    @classmethod
    def from_dict(cls, d: dict) -> "Plan":
        return cls(goal=d["goal"], roadmap=d.get("roadmap", []),
                   tasks=[Task(**t) for t in d.get("tasks", [])], risks=d.get("risks", []),
                   questions_for_owner=d.get("questions_for_owner", []), created=d.get("created", time.time()))

    def next_task(self) -> Task | None:
        done = {t.id for t in self.tasks if t.status == "done"}
        for t in self.tasks:
            if t.status == "todo" and all(d in done for d in t.depends_on):
                return t
        return None

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
    def __init__(self, root: Path, cfg: ProjectConfig, profile: Profile,
                 report: Reporter | None = None, *, dry_run: bool = False):
        self.root = root
        self.cfg = cfg
        self.profile = profile
        self.report = report or (lambda lvl, msg: None)
        self.dry_run = dry_run
        self.run_id = new_run_id()
        self.run_dir = root / GUILD_DIR / "runs" / self.run_id
        self.trace = Trace(self.run_dir / "trace.jsonl", self.run_id)
        self.tracker = CostTracker()
        self.router = Router(profile, self.tracker,
                             on_fallback=lambda slot, m, why: self._on_fallback(slot, m, why))
        self.sandbox = make_sandbox(root, cfg.sandbox, cfg.docker_image)
        self._roles: dict[str, Role] = {}
        self.trace.emit("run_start", profile=profile.name, project=str(root), config=cfg.model_dump())

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
        readonly = not any(t in mutating for t in role.tools) or role.name in {"critic", "security", "verifier"}
        return ToolContext(root=self.root, ignore=self.cfg.ignore, test_command=self.cfg.test_command,
                           lint_command=self.cfg.lint_command, sandbox=self.sandbox,
                           write_allowlist=role.write_allowlist, readonly=readonly)

    def agent(self, name: str, *, escalate: bool = False) -> Agent:
        role = self.role(name)
        return Agent(role, self.router, self._ctx(role), self.trace, escalate=escalate)

    def _phase(self, name: str, **info: Any) -> None:
        self.trace.emit("phase", phase=name, **info)
        self.report("phase", name)

    def close(self) -> None:
        self.trace.emit("run_end", total_usd=round(self.tracker.total_usd, 6),
                        usage=self.tracker.total_usage, by_role={k: [v[0], v[1], v[2]] for k, v in self.tracker.by_role().items()})
        self.trace.close()

    # ------------------------------------------------------------------ project context
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
        return f"Files:\n{listing}\n\nREADME (truncated):\n{readme or '(none)'}"

    # ------------------------------------------------------------------ plan
    def plan(self, goal: str, extra_context: str = "") -> Plan:
        self._phase("plan", goal=goal)
        res = self.agent("lead").run(
            f"PLAN the following goal for this project.\n\nGOAL:\n{goal}\n\n{extra_context}".strip(),
            context_blocks={"Project": self.project_summary()},
        )
        if not res.ok:
            raise RuntimeError(f"Lead did not return a plan. Raw reply:\n{res.raw[:2000]}")
        d = res.data
        tasks = [Task(id=t.get("id", f"T{i+1}"), title=t.get("title", ""), description=t.get("description", ""),
                      files=t.get("files", []), done_when=t.get("done_when", ""),
                      depends_on=t.get("depends_on", [])) for i, t in enumerate(d.get("tasks", []))]
        plan = Plan(goal=goal, roadmap=d.get("roadmap", []), tasks=tasks, risks=d.get("risks", []),
                    questions_for_owner=d.get("questions_for_owner", []))
        self.save_plan(plan)
        self.trace.emit("decision", role="lead", decision="plan", n_tasks=len(tasks), roadmap=plan.roadmap)
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
        r = subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True)
        if check and r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
        return (r.stdout + r.stderr).strip()

    def _has_git(self) -> bool:
        return (self.root / ".git").is_dir()

    def _start_branch(self, task: Task) -> str | None:
        if not self._has_git() or self.dry_run:
            return None
        slug = "".join(c if c.isalnum() else "-" for c in task.title.lower())[:40].strip("-")
        branch = f"guild/{task.id.lower()}-{slug}"
        if self._git("status", "--porcelain", "--", ".", f":(exclude){GUILD_DIR}", ":(exclude).gitignore"):
            self.report("warn", "working tree is dirty; guild will not create a branch. Commit or stash first.")
            return None
        self._git("checkout", "-B", branch, check=True)
        return branch

    def _commit(self, message: str) -> None:
        if not self._has_git() or self.dry_run:
            return
        self._git("add", "-A")
        if self._git("diff", "--cached", "--quiet") == "" and self._git("diff", "--cached", "--stat") == "":
            return
        self._git("commit", "-q", "-m", message)

    def _diff_text(self, base: str | None) -> str:
        if not self._has_git():
            return "(no git; review working tree)"
        return self._git("diff", base or "HEAD~1") or self._git("diff") or "(no diff)"

    # ------------------------------------------------------------------ run one task
    def run_task(self, plan: Plan, task: Task, *, roles: list[str] | None = None) -> TaskOutcome:
        enabled = roles or self.cfg.roles_enabled
        limits = self.profile.limits
        self._phase("task_start", task_id=task.id, title=task.title)
        task.status = "in_progress"
        self.save_plan(plan)

        base_ref = self._git("rev-parse", "HEAD") if self._has_git() else None
        task.branch = self._start_branch(task)
        cost0 = self.tracker.total_usd

        task_brief = (f"TASK {task.id}: {task.title}\n\n{task.description}\n\n"
                      f"Files likely involved: {', '.join(task.files) or 'unknown'}\n"
                      f"Done when: {task.done_when}")
        project_ctx = {"Project goal": plan.goal, "Project": self.project_summary()}

        feedback = ""
        verification = critic = security = lead_decision = docs = None
        escalated = False
        failed_rounds = 0
        accepted = False
        rounds = 0

        for rounds in range(1, limits.max_revision_rounds + 1):
            self._phase("implement", task_id=task.id, round=rounds, escalated=escalated)
            eng = self.agent("engineer", escalate=escalated).run(
                task_brief + (f"\n\nREVIEW FEEDBACK TO ADDRESS:\n{feedback}" if feedback else ""),
                context_blocks=project_ctx)
            self.trace.emit("decision", role="engineer", round=rounds, status=(eng.data or {}).get("status"),
                            summary=(eng.data or {}).get("summary", eng.raw[:300]))
            self._commit(f"guild({task.id}) round {rounds}: {task.title}")

            if eng.data and eng.data.get("status") == "blocked":
                task.status = "blocked"
                task.notes = eng.data.get("summary", "")
                self.report("warn", f"engineer blocked: {task.notes}")
                break

            # verify
            verification = None
            if "verifier" in enabled:
                self._phase("verify", task_id=task.id, round=rounds)
                ver = self.agent("verifier").run("Run the tests and report.", context_blocks=None)
                verification = ver.data or {"passed": False, "failures": ["verifier returned no JSON"], "output_tail": ver.raw[:500]}
                self.trace.emit("verification", round=rounds, **{k: verification.get(k) for k in ("passed", "tests_run", "failures")})
                self.report("info", f"verification: {'PASS' if verification.get('passed') else 'FAIL'}")

            diff = self._diff_text(base_ref)
            review_ctx = {"Task": task_brief, "Diff": diff[:30000],
                          "Verification": json.dumps(verification) if verification else "not run"}

            # review
            critic = security = None
            if "critic" in enabled:
                self._phase("critique", task_id=task.id, round=rounds)
                critic = (self.agent("critic").run("Review the change.", context_blocks=review_ctx)).data
            if "security" in enabled:
                self._phase("security", task_id=task.id, round=rounds)
                security = (self.agent("security").run("Review the change for security and privacy.",
                                                       context_blocks=review_ctx)).data

            passed = (verification is None) or bool(verification.get("passed"))
            critic_ok = critic is None or critic.get("verdict") == "approve"
            sec_ok = security is None or security.get("verdict") == "approve"
            sec_block = security is not None and security.get("verdict") == "block"

            if passed and critic_ok and sec_ok:
                accepted = True
                break

            failed_rounds += 1
            if limits.escalate_after and failed_rounds >= limits.escalate_after and not escalated \
                    and self.role("engineer").escalation_slot in self.profile.slots:
                escalated = True
                self.report("warn", "escalating engineer to stronger model")

            feedback = _compose_feedback(verification, critic, security)
            self.report("info", f"round {rounds}: changes requested" + (" (security BLOCK)" if sec_block else ""))

        # lead decision
        if accepted and "lead" in enabled:
            self._phase("lead_review", task_id=task.id)
            res = self.agent("lead").run(
                "REVIEW this task's outcome and decide.",
                context_blocks={"Task": task_brief, "Diff": self._diff_text(base_ref)[:30000],
                                "Verification": json.dumps(verification), "Critic": json.dumps(critic),
                                "Security": json.dumps(security)})
            lead_decision = res.data or {"decision": "REVISE", "notes": "lead returned no JSON"}
            self.trace.emit("decision", role="lead", decision=lead_decision.get("decision"),
                            notes=lead_decision.get("notes", ""))
            accepted = lead_decision.get("decision") == "ACCEPT"

        if accepted:
            task.status = "done"
            if "docs" in enabled:
                self._phase("docs", task_id=task.id)
                docs = (self.agent("docs").run("Update documentation for the accepted change.",
                                               context_blocks={"Task": task_brief,
                                                               "Diff": self._diff_text(base_ref)[:20000]})).data
                self._commit(f"guild({task.id}) docs")
        elif task.status != "blocked":
            task.status = "todo"
            task.notes = feedback[:2000]

        self.save_plan(plan)
        outcome = TaskOutcome(task=task, accepted=accepted, rounds=rounds, verification=verification,
                              critic=critic, security=security, lead_decision=lead_decision, docs=docs,
                              escalated=escalated, cost_usd=self.tracker.total_usd - cost0)
        self.trace.emit("task_end", task_id=task.id, accepted=accepted, rounds=rounds,
                        escalated=escalated, cost_usd=round(outcome.cost_usd, 6), branch=task.branch)
        return outcome

    # ------------------------------------------------------------------ one-off ask
    def ask(self, role_name: str, question: str) -> AgentResult:
        self._phase("ask", role=role_name)
        return self.agent(role_name).run(question, context_blocks={"Project": self.project_summary()})


def _compose_feedback(verification: dict | None, critic: dict | None, security: dict | None) -> str:
    parts: list[str] = []
    if verification and not verification.get("passed"):
        parts.append("TESTS FAILED:\n" + "\n".join(f"- {f}" for f in verification.get("failures", []))
                     + "\n\nOutput tail:\n" + str(verification.get("output_tail", ""))[:2000])
    for label, rev in (("CRITIC", critic), ("SECURITY", security)):
        if rev and rev.get("verdict") != "approve":
            items = rev.get("findings", [])
            parts.append(f"{label} ({rev.get('verdict')}): {rev.get('summary', '')}\n" +
                         "\n".join(f"- [{f.get('severity')}] {f.get('file')}: {f.get('issue')} → {f.get('suggestion') or f.get('fix')}"
                                   for f in items))
    return "\n\n".join(parts) or "No specific feedback."
