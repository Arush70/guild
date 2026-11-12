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
import threading
import traceback
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from functools import partial
from pathlib import Path
from typing import Any

from . import projectdocs
from .agent import Agent, AgentResult
from .config import Profile, ProjectConfig, Role, load_role
from .errors import Cancelled
from .paths import ProjectPaths
from .plan import Plan, PlanStore, Task
from .providers import CostTracker, Message, Router
from .providers.router import load_prices
from .repo import BRANCH_PREFIX, Repo, TaskBranch, files_in_diff
from .sandbox import make_sandbox
from .tools import REGISTRY, ToolContext, Workspace
from .trace import Trace, new_run_id
from .verify import Verification, Verifier

# Roles that must never change the project, even when a project overrides their YAML and leaves
# out `readonly: true`.
READONLY_ROLES = frozenset({"critic", "security", "verifier", "assistant", "performance"})

# Interactive pair programming: what needs the owner's approval.
PAIR_MODES = {
    "ask": "ASK: the owner approves each file change and command.",
    "auto-edits": "AUTO-EDITS: file changes are applied directly; commands need the owner's "
    "approval.",
    "plan": "PLAN: read-only. Explore, then reply with a step-by-step plan. Do not call "
    "write_file, edit_file or commands that change anything.",
}


# Reviewer roles of the task loop: role -> (phase name in the trace, instruction).
REVIEWERS = {
    "critic": ("critique", "Review the change."),
    "security": ("security", "Review the change for security and privacy."),
}


class PairSession:
    """What a pair-programming conversation is allowed to do without asking: its mode, and
    whether the owner has said "approve the remaining edits"."""

    def __init__(self, mode: str = "ask"):
        self.mode = mode
        self.all_edits = False

    @property
    def mode(self) -> str:
        return self._mode

    @mode.setter
    def mode(self, mode: str) -> None:
        if mode not in PAIR_MODES:
            raise ValueError(f"mode must be one of {list(PAIR_MODES)}, not {mode!r}")
        self._mode = mode
        self.all_edits = False

    @property
    def readonly(self) -> bool:
        return self.mode == "plan"

    def approves(self, request: dict) -> bool:
        """True when this request goes through without asking the owner."""
        return request.get("kind") == "edit" and (self.all_edits or self.mode == "auto-edits")


@dataclass
class TaskOutcome:
    task: Task
    accepted: bool
    rounds: int
    verification: Verification | None
    reviews: dict[str, dict]  # reviewer role -> its last review
    lead_decision: dict | None
    docs: dict | None
    escalated: bool
    cost_usd: float

    def to_dict(self) -> dict:
        """The shape sent to front ends and stored in job results: one key per reviewer
        (None when that reviewer did not run)."""
        d = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "reviews"}
        return {**d, **{name: self.reviews.get(name) for name in REVIEWERS}}


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
        on_event: Callable[[dict], None] | None = None,
    ):
        """report(level, message): progress for a human. on_token(role, chunk): model output as
        it streams. on_event(event): every trace event as it is written."""
        self.root = root
        self.cfg = cfg
        self.profile = profile
        self.report = report or (lambda lvl, msg: None)
        self.dry_run = dry_run
        self.on_token = on_token
        self.paths = ProjectPaths(root)
        self.plans = PlanStore(self.paths.plan)
        self.repo = Repo(root)
        self.run_id = new_run_id()
        self.trace = Trace(self.paths.trace(self.run_id), self.run_id, echo=on_event)
        self.tracker = CostTracker()
        self.tracker.prices.update(load_prices(self.paths.prices))
        # set by cancel(): checked before every model call, between tool calls, while a
        # command runs and while waiting for an approval
        self.cancel_event = threading.Event()
        self.router = Router(
            profile, self.tracker, on_fallback=self._on_fallback, cancel=self.cancel_event
        )
        self.sandbox = make_sandbox(root, cfg.sandbox, cfg.docker_image, self.cancel_event)
        self.verifier = Verifier(self.sandbox, cfg.test_command, cfg.lint_command)
        self._roles: dict[str, Role] = {}
        # files written so far in the current task: later rounds may overwrite them unread
        self._task_written: set[str] = set()
        self.trace.emit(
            "run_start", profile=profile.name, project=str(root), config=cfg.model_dump()
        )

    # ------------------------------------------------------------------ infra
    def _on_fallback(self, slot: str, model: str, why: str) -> None:
        self.trace.emit("fallback", slot=slot, model=model, reason=why)
        # level "fallback", like "phase": text for a terminal; front ends show the event instead
        self.report("fallback", f"{model} failed ({why}); trying next candidate for '{slot}'")

    def _warn(self, msg: str, role: str = "guild") -> None:
        """A warning for the owner that also belongs in the trace, under the role it is about."""
        self.trace.emit("note", role=role, msg=msg[:400])
        self.report("warn", msg)

    def role(self, name: str) -> Role:
        if name not in self._roles:
            self._roles[name] = load_role(name, self.root)
        return self._roles[name]

    def _ctx(self, role: Role) -> ToolContext:
        readonly = role.readonly
        if readonly is None:
            can_change = any(REGISTRY[t].mutating for t in role.tools if t in REGISTRY)
            readonly = role.name in READONLY_ROLES or not can_change
        return ToolContext(
            root=self.root,
            ignore=self.cfg.ignore,
            test_command=self.cfg.test_command,
            lint_command=self.cfg.lint_command,
            sandbox=self.sandbox,
            write_allowlist=role.write_allowlist,
            readonly=readonly,
            files_read=set(self._task_written),
        )

    def agent(self, name: str, *, escalate: bool = False) -> Agent:
        role = self.role(name)
        limits = self.profile.limits
        return Agent(
            role,
            self.router,
            self._ctx(role),
            self.trace,
            escalate=escalate,
            on_token=self.on_token,
            max_tool_calls=limits.max_tool_calls_per_task if role.task_budget else None,
        )

    def _phase(self, name: str, role: str | None = None, **info: Any) -> None:
        """A new step begins. `role` is who works in it (front ends light that role up)."""
        self.trace.emit("phase", phase=name, role=role, **info)
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
    def project_summary(self) -> str:
        """What every role is told about the project: its files, README and project docs."""
        readme = next(
            (p for n in ("README.md", "README.rst", "README") if (p := self.root / n).is_file()),
            None,
        )
        parts = [
            f"Files:\n{Workspace(self.root, self.cfg.ignore).listing('.', 200)}",
            "README (truncated):\n"
            + (readme.read_text(encoding="utf-8", errors="replace")[:4000] if readme else "(none)"),
            *(f"{label}:\n{text}" for label, text in projectdocs.read(self.root).items()),
        ]
        return "\n\n".join(parts)

    # ------------------------------------------------------------------ plan
    def _lead_plan(self, prompt: str, context: dict[str, str]) -> dict:
        res = self.agent("lead").run(prompt, context_blocks=context, job="plan")
        if not res.ok:
            raise RuntimeError(f"Lead did not return a plan. Raw reply:\n{res.raw[:2000]}")
        return res.data

    def plan(self, goal: str, extra_context: str = "") -> Plan:
        self._phase("plan", "lead", goal=goal)
        data = self._lead_plan(
            f"PLAN the following goal for this project.\n\nGOAL:\n{goal}\n\n"
            f"{extra_context}".strip(),
            {"Project": self.project_summary()},
        )
        plan = Plan.from_lead(goal, data)
        self.plans.save(plan)
        self.trace.emit(
            "decision", role="lead", decision="plan", n_tasks=len(plan.tasks), roadmap=plan.roadmap
        )
        return plan

    def revise_plan(self, instruction: str) -> Plan:
        """Ask the Lead to rewrite the current plan according to a natural-language instruction."""
        current = self.plans.load()
        if current is None:
            return self.plan(instruction)
        self._phase("revise_plan", "lead", instruction=instruction)
        data = self._lead_plan(
            "REVISE the current plan according to the owner's instruction. Keep task ids of tasks "
            "that stay unchanged; keep status/branch of tasks already done. Reply with the FULL "
            "updated plan in the PLAN JSON format.\n\nOWNER'S INSTRUCTION:\n" + instruction,
            {
                "Project goal": current.goal,
                "Current plan (JSON)": json.dumps(current.to_dict(), indent=1),
                "Project": self.project_summary(),
            },
        )
        plan = Plan.from_lead(current.goal, data, previous=current)
        self.plans.save(plan)
        self.trace.emit("decision", role="lead", decision="revise_plan", n_tasks=len(plan.tasks))
        return plan

    # ------------------------------------------------------------------ run one task
    def run_task(self, plan: Plan, task: Task, *, roles: list[str] | None = None) -> TaskOutcome:
        branch: TaskBranch | None = None
        try:
            self._phase("task_start", task_id=task.id, title=task.title)
            task.status = "in_progress"
            self.plans.save(plan)
            branch = TaskBranch.start(
                self.repo,
                task.id,
                task.title,
                dry_run=self.dry_run,
                warn=partial(self.report, "warn"),
            )
            return self._run_task(plan, task, branch, roles or self.cfg.roles_enabled)
        except BaseException as e:  # incl. KeyboardInterrupt: Ctrl+C must not strand the task
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
                self.plans.save(plan)
            if branch:
                branch.leave()
            raise

    def _run_task(
        self, plan: Plan, task: Task, branch: TaskBranch, enabled: list[str]
    ) -> TaskOutcome:
        limits = self.profile.limits
        task.branch = branch.name
        if branch.name:
            # Where to merge back to. A task stacked on another guild branch (run all) inherits
            # that task's base, so it is never merged into a throwaway branch.
            base = branch.started_from
            if base and base.startswith(BRANCH_PREFIX):
                under = next((t for t in plan.tasks if t.branch == base and t is not task), None)
                base = under.base if under else None  # None: merge into the default branch
            task.base = base
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

        self._task_written = set()
        baseline = self.verifier.baseline() if "verifier" in enabled else None

        feedback = ""
        verification = lead_decision = docs = None
        reviews: dict[str, dict] = {}
        escalated = False
        failed_rounds = 0
        accepted = False
        rounds = 0
        pushed_back_on_block = False

        for rounds in range(1, limits.max_revision_rounds + 1):
            self._phase("implement", "engineer", task_id=task.id, round=rounds, escalated=escalated)
            eng = self._implement(
                self.agent("engineer", escalate=escalated),
                task_brief,
                feedback,
                project_ctx,
                lambda n=rounds: branch.commit(f"guild({task.id}) round {n}: {task.title}"),
                push_back=not pushed_back_on_block,
            )
            pushed_back_on_block = pushed_back_on_block or eng.pop("pushed_back", False)
            self.trace.emit(
                "decision",
                role="engineer",
                round=rounds,
                status=eng.get("status"),
                summary=eng.get("summary", ""),
            )
            if eng.get("status") == "blocked":
                task.status = "blocked"
                task.notes = str(eng.get("summary") or "")
                self.report("warn", f"engineer blocked: {task.notes}")
                break

            verification = None
            if baseline is not None:
                self._phase("verify", "verifier", task_id=task.id, round=rounds)
                verification = self.verifier.run(baseline)
                self.trace.emit(
                    "verification",
                    round=rounds,
                    passed=verification.passed,
                    tests_run=verification.tests_run,
                    failures=verification.failures,
                    exit_code=verification.exit_code,
                    removed_tests=verification.removed_tests or None,
                )
                v_status = "PASS" if verification.passed else "FAIL"
                self.report("info", f"verification: {v_status} ({verification.tests_run} tests)")

            diff = branch.review_diff()
            self.trace.emit(
                "review_input",
                round=rounds,
                diff_chars=len(diff),
                files_in_diff=files_in_diff(diff),
            )
            review_ctx = {
                "Task": task_brief,
                "Diff": diff[:30000],
                "Verification": json.dumps(asdict(verification)) if verification else "not run",
            }
            reviews = {
                role: self._review(role, review_ctx, task, rounds)
                for role in REVIEWERS
                if role in enabled
            }

            tests_ok = verification is None or verification.passed
            if tests_ok and all(r.get("verdict") == "approve" for r in reviews.values()):
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

            feedback = _compose_feedback(verification, reviews)
            blocking = [r for r, review in reviews.items() if review.get("verdict") == "block"]
            self.report(
                "info",
                f"round {rounds}: changes requested"
                + (f" ({', '.join(blocking)} BLOCK)" if blocking else ""),
            )

        if accepted and "lead" in enabled:
            self._phase("lead_review", "lead", task_id=task.id)
            res = self.agent("lead").run(
                "REVIEW this task's outcome and decide.",
                context_blocks={
                    "Task": task_brief,
                    "Diff": branch.review_diff()[:30000],
                    "Verification": json.dumps(asdict(verification) if verification else None),
                    **{name.title(): json.dumps(reviews.get(name)) for name in REVIEWERS},
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
                self._phase("docs", "docs", task_id=task.id)
                docs = (
                    self.agent("docs").run(
                        "Update documentation for the accepted change.",
                        context_blocks={"Task": task_brief, "Diff": branch.review_diff()[:20000]},
                    )
                ).data
                branch.commit(f"guild({task.id}) docs")
        elif task.status != "blocked":
            task.status = "todo"
            notes = feedback
            if lead_decision:
                # the Lead's reason is the only explanation the owner gets for a rejection
                notes = "\n".join(
                    [
                        f"LEAD {lead_decision.get('decision')}: {lead_decision.get('notes', '')}",
                        *(f"- {i}" for i in lead_decision.get("improvements") or []),
                    ]
                )
            task.notes = notes[:2000]

        self.plans.save(plan)
        if not accepted:
            branch.leave()
        outcome = TaskOutcome(
            task=task,
            accepted=accepted,
            rounds=rounds,
            verification=verification,
            reviews=reviews,
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

    def _implement(
        self,
        agent: Agent,
        brief: str,
        feedback: str,
        context: dict[str, str],
        commit: Callable[[], None],
        *,
        push_back: bool,
    ) -> dict:
        """The engineer's part of one round. Returns its reply ({"status", "summary", …}).

        Small models fail here in two known ways, each answered once: "done" with no file
        changed (the code was written as prose in the reply), and "blocked" on something that
        is not an obstacle (a missing file, a failing test). `pushed_back` is set in the reply
        when the second answer was used, so a task gets it only once.
        """
        review = f"\n\nREVIEW FEEDBACK TO ADDRESS:\n{feedback}" if feedback else ""

        def attempt(extra: str) -> dict:
            res = agent.run(brief + extra, context_blocks=context)
            self._task_written |= agent.ctx.files_written
            commit()
            return {"summary": res.raw[:300], **(res.data or {})}

        def no_changes(reply: dict) -> bool:
            return reply.get("status") == "done" and not agent.ctx.files_written

        eng = attempt(review)
        if no_changes(eng):
            self._warn(
                "engineer reported done but changed no files — asking it to use the tools",
                role="engineer",
            )
            eng = attempt(
                "\n\nIMPORTANT: your previous reply described code but did not change any "
                "file. Code written in the reply is DISCARDED. The ONLY way to change the "
                "project is the write_file / edit_file tools. Call them now with the full file "
                "contents, run run_tests, then reply with the final JSON." + review
            )
            if no_changes(eng):
                eng = {
                    "status": "blocked",
                    "summary": "engineer produced no file changes after two attempts "
                    "(model wrote code as text)",
                }
        if eng.get("status") == "blocked" and push_back:
            why = str(eng.get("summary") or "")
            self._warn(f"engineer says blocked ({why[:120]}) — pushing back once", role="engineer")
            eng = attempt(
                "\n\nYou reported BLOCKED because: "
                + why
                + "\n\nThat is not a valid reason. Facts: a file that does not exist must "
                "be CREATED with write_file (this is normal for new projects). A failing or "
                "missing test must be fixed or written by you. You have all the tools you "
                "need. Implement the task now: create or edit the files, run run_tests, then "
                'reply with the final JSON. Use "blocked" only if a tool call literally fails '
                "and you can quote its error."
            )
            if no_changes(eng):
                eng = {
                    "status": "blocked",
                    "summary": "engineer produced no file changes (after push-back)",
                }
            eng["pushed_back"] = True
        return eng

    def _review(self, role: str, context: dict[str, str], task: Task, rounds: int) -> dict:
        phase, prompt = REVIEWERS[role]
        self._phase(phase, role, task_id=task.id, round=rounds)
        review = self.agent(role).run(prompt, context_blocks=context).data
        if review is None:
            # parse and repairs failed: silence is never an approval
            self._warn(f"{role} returned no valid JSON; treating as changes requested", role=role)
            review = {
                "verdict": "request_changes",
                "summary": f"{role} did not produce a valid review (no JSON reply)",
                "findings": [],
            }
        return review

    # ------------------------------------------------------------------ run several
    def run_tasks(
        self,
        plan: Plan,
        task_ids: list[str] | None = None,
        *,
        all_tasks: bool = False,
        roles: list[str] | None = None,
        stop_on_failure: bool | None = None,
        before: Callable[[Task], bool] | None = None,
        after: Callable[[TaskOutcome], bool] | None = None,
    ) -> list[TaskOutcome]:
        """Run the given task ids in order, or the next runnable task (every runnable task with
        `all_tasks`).

        stop_on_failure: stop after a task that was not accepted. Default: explicitly selected
        tasks all get their turn (a failure only skips its dependants); "run all" stops.
        before / after: called around each task; returning False stops the run (the CLI asks
        the owner here).
        """
        selected = plan.require(*task_ids) if task_ids else None
        if stop_on_failure is None:
            stop_on_failure = selected is None
        failed: set[str] = set()
        outcomes: list[TaskOutcome] = []

        def queue():
            if selected is None:
                while task := plan.next_task(skip=failed):
                    yield task
                    if not all_tasks:
                        return
                return
            for task in selected:
                if task.status == "done":
                    continue
                waiting = set(plan.unmet_dependencies(task)) | (set(task.depends_on) & failed)
                if waiting:
                    self.report(
                        "warn",
                        f"skipping {task.id}: depends on {', '.join(sorted(waiting))} "
                        "which is not done yet",
                    )
                    continue
                yield task

        for task in queue():
            if before and not before(task):
                break
            out = self.run_task(plan, task, roles=roles)
            outcomes.append(out)
            if not out.accepted:
                failed.add(task.id)
            if after and not after(out):
                break
            if not out.accepted and stop_on_failure:
                break
        return outcomes

    # ------------------------------------------------------------------ conversations
    @staticmethod
    def _prior(history: list[dict] | None, turns: int) -> list[Message]:
        """Earlier chat turns as messages. history: [{"role": "user" | "assistant", "content"}]"""
        return [
            Message(h["role"], str(h["content"]))
            for h in (history or [])[-turns:]
            if isinstance(h, dict) and h.get("role") in ("user", "assistant") and h.get("content")
        ]

    def chat(self, question: str, history: list[dict] | None = None) -> AgentResult:
        """Read-only conversation about the project."""
        self._phase("chat", "assistant")
        plan = self.plans.load()
        blocks = {"Project": self.project_summary()}
        if plan:
            blocks["Current plan"] = json.dumps(plan.to_dict(), indent=1)[:6000]
        return self.agent("assistant").run(
            question, context_blocks=blocks, prior=self._prior(history, 12), expect_json=False
        )

    def pair(
        self,
        message: str,
        history: list[dict] | None = None,
        *,
        session: PairSession,
        ask: Callable[[dict], tuple[bool, str]] | None = None,
    ) -> AgentResult:
        """One turn of interactive pair programming in the owner's working tree.

        Unlike run_task there is no branch, no commit and no review round: the owner is the
        reviewer, live. Before every file change and every non-inspection command the session
        decides whether it may go ahead; otherwise `ask(request) -> (approved, reason)` puts it
        to the owner. With no `ask`, nothing that needs approval happens: the gate fails closed.
        """
        self._phase("pair", "pair", mode=session.mode)
        agent = self.agent("pair")
        ctx = agent.ctx
        ctx.readonly = session.readonly

        def gate(req: dict) -> tuple[bool, str]:
            auto = session.approves(req)
            if auto:
                approved, why = True, "allowed by the session"
            elif ask is None:
                approved, why = False, "nobody is available to approve it"
            else:
                approved, why = ask(req)
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
        res = agent.run(
            message,
            context_blocks={"Project": self.project_summary(), "Mode": PAIR_MODES[session.mode]},
            prior=self._prior(history, 16),
            expect_json=False,
        )
        res.data = {
            "text": res.raw,
            "files_changed": sorted(ctx.files_written),
            "mode": session.mode,
        }
        self.trace.emit(
            "decision",
            role="pair",
            decision="reply",
            summary=res.raw[:300],
            files=res.data["files_changed"],
        )
        return res

    def ask(self, role_name: str, question: str) -> AgentResult:
        self._phase("ask", role_name)
        return self.agent(role_name).run(
            question, context_blocks={"Project": self.project_summary()}
        )


def _compose_feedback(verification: Verification | None, reviews: dict[str, dict]) -> str:
    parts: list[str] = []
    if verification and not verification.passed:
        parts.append(
            "TESTS FAILED:\n"
            + "\n".join(f"- {f}" for f in verification.failures)
            + "\n\nOutput tail:\n"
            + verification.output_tail[:2000]
        )
    for role, review in reviews.items():
        if review.get("verdict") != "approve":
            parts.append(
                f"{role.upper()} ({review.get('verdict')}): {review.get('summary', '')}\n"
                + "\n".join(
                    f"- [{f.get('severity')}] {f.get('file')}: {f.get('issue')} → "
                    f"{f.get('suggestion') or f.get('fix')}"
                    for f in review.get("findings", [])
                )
            )
    return "\n\n".join(parts) or "No specific feedback."
