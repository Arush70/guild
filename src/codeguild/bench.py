"""`codeguild bench`: the Phase 1 check, as one command.

Builds the calculator demo in a fresh folder, lets the team plan and implement it, merges every
accepted task, and then checks the result with acceptance tests the agents never saw. Repeats
that N times and writes a report. Run it with real models: with scripted ones it only proves
the plumbing.

What counts as a pass for one run: every planned task accepted and merged, the project's own
tests pass on the base branch afterwards, and every hidden acceptance check passes.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import Profile, ProjectConfig, save_project_config
from .errors import Cancelled
from .plan import Plan, Task
from .repo import Repo
from .trace import read_trace, summarize
from .workflow import Guild, TaskOutcome

GOAL = (
    "Build a small calculator library in app.py with four functions: add(a, b), "
    "subtract(a, b), multiply(a, b) and divide(a, b). divide must raise ValueError when b is 0. "
    "Each function needs pytest tests in test_app.py. Keep the existing add function and its test."
)

STARTER = {
    "app.py": 'def add(a, b):\n    """Return a + b."""\n    return a + b\n',
    "test_app.py": "from app import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
    ".gitignore": ".codeguild/\n__pycache__/\n.pytest_cache/\n",
}

# The same work as GOAL, already split: measures the engineer and the reviewers without the
# lead's planning.
FIXED_TASKS = [
    ("subtract", "Add subtract(a, b) to app.py, returning a - b, with a test in test_app.py."),
    ("multiply", "Add multiply(a, b) to app.py, returning a * b, with a test in test_app.py."),
    (
        "divide",
        "Add divide(a, b) to app.py, returning a / b and raising ValueError when b == 0, with "
        "tests for both cases in test_app.py.",
    ),
    (
        "tests",
        "Make sure test_app.py has at least one test for each of add, subtract, multiply and "
        "divide, including divide by zero. Add any test that is missing; change no behaviour.",
    ),
]

# Run in the finished project, outside the agents' view: one line per check, "name ok|FAIL".
ACCEPTANCE = textwrap.dedent(
    """
    import importlib, sys
    sys.path.insert(0, ".")
    def check(name, fn):
        try:
            ok = bool(fn())
        except Exception:
            ok = False
        print(name, "ok" if ok else "FAIL")
    app = importlib.import_module("app")
    check("add", lambda: app.add(2, 3) == 5 and app.add(-1, 1) == 0)
    check("subtract", lambda: app.subtract(5, 3) == 2 and app.subtract(3, 5) == -2)
    check("multiply", lambda: app.multiply(4, 3) == 12 and app.multiply(-2, 3) == -6)
    check("divide", lambda: app.divide(9, 3) == 3 and abs(app.divide(1, 4) - 0.25) < 1e-9)
    def zero():
        try:
            app.divide(1, 0)
        except ValueError:
            return True
        return False
    check("divide by zero", zero)
    """
)


@dataclass
class TaskResult:
    id: str
    title: str
    accepted: bool
    merged: bool
    rounds: int
    usd: float
    why_not: str = ""


@dataclass
class RunResult:
    index: int
    folder: str
    passed: bool = False
    tasks: list[TaskResult] = field(default_factory=list)
    tests_pass_after_merge: bool = False
    acceptance: dict[str, bool] = field(default_factory=dict)
    wall_s: float = 0.0
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usd: float = 0.0
    trace: str = ""
    error: str = ""


def make_project(folder: Path, profile: Profile, test_command: str) -> ProjectConfig:
    """The demo's starting point: add() and its test, committed, codeguild set up."""
    folder.mkdir(parents=True)
    for name, text in STARTER.items():
        (folder / name).write_text(text, encoding="utf-8", newline="")
    repo = Repo.init(folder)
    if repo is None:
        raise RuntimeError("git is needed for the bench (tasks run on branches)")
    cfg = ProjectConfig(profile=profile.name, test_command=test_command)
    save_project_config(folder, cfg)
    repo.commit_all("starting point")
    return cfg


def _why_not(out: TaskOutcome) -> str:
    if out.accepted:
        return ""
    v = out.verification
    if v is not None and not v.passed:
        return "tests: " + "; ".join(v.failures[:2])[:300]
    rejected = [
        f"{name}: {(r or {}).get('summary', '')}"
        for name, r in out.reviews.items()
        if r and r.get("verdict") not in (None, "approve")
    ]
    if rejected:
        return "; ".join(rejected)[:300]
    decision = out.lead_decision or {}
    return (decision.get("notes") or out.task.notes or "lead did not accept")[:300]


def bench_once(
    index: int,
    folder: Path,
    profile: Profile,
    *,
    fixed_plan: bool,
    test_command: str,
    timeout_s: float,
    report: Callable[[str, str], None],
) -> RunResult:
    result = RunResult(index, str(folder))
    started = time.monotonic()
    cfg = make_project(folder, profile, test_command)
    g = Guild(folder, cfg, profile, report=report)
    timer = threading.Timer(timeout_s, g.cancel)
    timer.daemon = True
    timer.start()
    try:
        if fixed_plan:
            plan = Plan(
                goal=GOAL,
                roadmap=[],
                tasks=[
                    Task(
                        id=f"T{i}",
                        title=title,
                        description=text,
                        done_when="pytest passes",
                        depends_on=[f"T{i - 1}"] if i > 1 else [],
                        files=["app.py", "test_app.py"],
                    )
                    for i, (title, text) in enumerate(FIXED_TASKS, 1)
                ],
            )
            g.plans.save(plan)
        else:
            plan = g.plan(GOAL)

        def merge(out: TaskOutcome) -> bool:
            task = out.task
            merged = False
            if out.accepted and task.branch:
                done = g.repo.merge_branch(task.branch, task.base)
                merged = bool(done.get("ok"))
                if merged:
                    plan.branch_merged(task.id)
                    g.plans.save(plan)
            result.tasks.append(
                TaskResult(
                    task.id,
                    task.title,
                    out.accepted,
                    merged,
                    out.rounds,
                    out.cost_usd,
                    _why_not(out) or ("" if merged else "accepted but the merge failed"),
                )
            )
            return merged  # the next task must build on this one

        g.run_tasks(plan, all_tasks=True, after=merge)
        for task in plan.tasks:  # never started: an earlier task stopped the run
            if not any(t.id == task.id for t in result.tasks):
                result.tasks.append(
                    TaskResult(task.id, task.title, False, False, 0, 0.0, "not reached")
                )
    except Cancelled:
        result.error = f"stopped after {timeout_s / 60:.0f} minutes"
    except Exception as e:  # a bench reports a broken run, it does not end on it
        result.error = f"{type(e).__name__}: {e}"[:500]
    finally:
        timer.cancel()
        g.close()

    result.wall_s = round(time.monotonic() - started, 1)
    totals = summarize(read_trace(g.trace.path))
    result.calls, result.usd = totals["calls"], totals["usd"]
    result.input_tokens, result.output_tokens = totals["input_tokens"], totals["output_tokens"]
    result.trace = str(g.trace.path)

    if g.repo.current_branch() != "main":
        g.repo.run("checkout", "-q", "main")
    result.tests_pass_after_merge = _run(test_command, folder).returncode == 0
    script = folder.parent / "acceptance.py"  # beside the project: the agents never saw it
    script.write_text(ACCEPTANCE, encoding="utf-8")
    checks = subprocess.run(
        [sys.executable, str(script)],
        cwd=folder,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        stdin=subprocess.DEVNULL,
    )
    for line in checks.stdout.splitlines():
        name, _, verdict = line.rpartition(" ")
        if name:
            result.acceptance[name] = verdict == "ok"
    if not result.acceptance:
        result.acceptance = {"app.py imports": False}
    result.passed = (
        not result.error
        and bool(result.tasks)
        and all(t.accepted and t.merged for t in result.tasks)
        and result.tests_pass_after_merge
        and all(result.acceptance.values())
    )
    return result


def _run(command: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        shell=True,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
        stdin=subprocess.DEVNULL,
    )


def write_report(results: list[RunResult], folder: Path, profile: Profile) -> Path:
    """report.json (everything) and report.md (for a person, or to paste into an issue)."""
    (folder / "report.json").write_text(
        json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8"
    )
    passed = sum(r.passed for r in results)
    lines = [
        f"# codeguild bench: {passed}/{len(results)} runs passed",
        "",
        f"Profile `{profile.name}`: "
        + ", ".join(f"{slot} = {chain[0]}" for slot, chain in profile.slots.items() if chain),
        "",
        "| run | result | tasks accepted | tests after merge | hidden checks | time | calls "
        "| tokens in/out | cost |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        accepted = sum(t.accepted and t.merged for t in r.tasks)
        hidden = sum(r.acceptance.values())
        lines.append(
            f"| {r.index} | {'pass' if r.passed else 'FAIL'} | {accepted}/{len(r.tasks)} "
            f"| {'pass' if r.tests_pass_after_merge else 'fail'} "
            f"| {hidden}/{len(r.acceptance)} | {r.wall_s / 60:.1f} min | {r.calls} "
            f"| {r.input_tokens:,}/{r.output_tokens:,} | ${r.usd:.4f} |"
        )
    problems = [(r, t) for r in results for t in r.tasks if not (t.accepted and t.merged)]
    if problems or any(r.error for r in results):
        lines += ["", "## What went wrong", ""]
        for r in results:
            if r.error:
                lines.append(f"- run {r.index}: {r.error}")
        for r, t in problems:
            lines.append(f"- run {r.index}, {t.id} {t.title} ({t.rounds} rounds): {t.why_not}")
    lines += ["", "Traces (send these with the report):", ""]
    lines += [f"- run {r.index}: `{r.trace}`" for r in results]
    path = folder / "report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
