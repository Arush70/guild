"""guild — command line."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .config import (
    GUILD_DIR,
    ProjectConfig,
    find_project_root,
    list_available,
    load_profile,
    load_project_config,
    load_role,
    save_project_config,
)
from .providers import AllProvidersFailed, BudgetExceeded
from .providers.openai_compat import ENDPOINTS

app = typer.Typer(
    help="A budget-aware multi-agent coding team.", no_args_is_help=True, rich_markup_mode="rich"
)
console = Console()
err = Console(stderr=True)


def _report(level: str, msg: str) -> None:
    style = {"phase": "bold cyan", "warn": "yellow", "info": "dim", "error": "bold red"}.get(
        level, ""
    )
    prefix = {"phase": "▶", "warn": "!", "info": "·", "error": "✖"}.get(level, "")
    console.print(f"[{style}]{prefix} {escape(msg)}[/{style}]")  # model text may contain [..]


def _load(project: Path | None, profile_override: str | None):
    root = find_project_root(project)
    cfg = load_project_config(root)
    if profile_override:
        cfg.profile = profile_override
    try:
        profile = load_profile(cfg.profile, root)
    except FileNotFoundError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(2) from None
    return root, cfg, profile


def _guild(root, cfg, profile, dry_run: bool = False):
    from .workflow import Guild

    return Guild(root, cfg, profile, report=_report, dry_run=dry_run)


def _cost_table(tracker) -> Table:
    t = Table(title="Cost", show_lines=False)
    t.add_column("role")
    t.add_column("calls", justify="right")
    t.add_column("in tok", justify="right")
    t.add_column("out tok", justify="right")
    t.add_column("USD", justify="right")
    for role, (usage, usd, n) in sorted(tracker.by_role().items()):
        t.add_row(role, str(n), f"{usage.input_tokens:,}", f"{usage.output_tokens:,}", f"{usd:.4f}")
    u = tracker.total_usage
    t.add_row(
        "[bold]total",
        f"[bold]{len(tracker.calls)}",
        f"[bold]{u.input_tokens:,}",
        f"[bold]{u.output_tokens:,}",
        f"[bold]{tracker.total_usd:.4f}",
    )
    return t


# ----------------------------------------------------------------------------- commands


def _version_cb(value: bool):
    if value:
        console.print(f"guild {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def _main(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", "-V", is_eager=True, callback=_version_cb),
):
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())


@app.command()
def init(
    path: Path = typer.Argument(Path("."), help="project directory"),
    profile: str | None = typer.Option(
        None, "--profile", "-p", help="free | lite | pro (skip the question)"
    ),
    test_command: str | None = typer.Option(
        None, help="how to run tests (auto-detected if omitted)"
    ),
    sandbox: str = typer.Option("local", help="local | docker"),
    yes: bool = typer.Option(False, "--yes", "-y", help="accept all defaults, no questions"),
    docs: bool | None = typer.Option(
        None,
        "--docs/--no-docs",
        help="scaffold docs/PRD.md, ARCHITECTURE.md, RULES.md, TASKS.md, MEMORY.md, DECISIONS.md",
    ),
):
    """Set up guild for a project: detects your hardware, keys and test runner, picks models
    that fit, and writes .guild/config.yaml plus a tailored profile. Optionally scaffolds the
    project documents every role reads for context."""
    import yaml

    from . import hardware as hwmod

    root = path.resolve()
    root.mkdir(parents=True, exist_ok=True)
    console.print(Panel("\n".join(hwmod.summary_lines(hw := hwmod.detect())), title="your machine"))

    # -- tier
    if profile is None:
        if yes:
            profile = "lite" if hw.keys.get("anthropic") else "free"
        else:
            console.print(
                "[bold]Which profile?[/bold]\n  [cyan]free[/cyan]  £0 — local Ollama models"
                + (
                    " + your free cloud keys"
                    if hw.keys.get("groq") or hw.keys.get("gemini")
                    else ""
                )
                + "\n  [cyan]lite[/cyan]  ~£10–20/mo — Claude plans & reviews, local model codes "
                "(needs ANTHROPIC_API_KEY)"
                "\n  [cyan]pro[/cyan]   pay-as-you-go — best model everywhere "
                "(needs Anthropic/OpenAI/Gemini keys)"
            )
            default = "lite" if hw.keys.get("anthropic") else "free"
            profile = typer.prompt("profile", default=default).strip().lower()
    if profile not in {"free", "lite", "pro"}:
        err.print(f"[red]unknown profile '{profile}'[/red]")
        raise typer.Exit(2)
    if profile in {"lite", "pro"} and not hw.keys.get("anthropic"):
        console.print(
            "[yellow]note: ANTHROPIC_API_KEY is not set — Claude seats will fall back to other "
            "candidates until you set it.[/yellow]"
        )

    # -- test command
    detected = hwmod.detect_test_command(root)
    if test_command is None:
        test_command = detected if yes else typer.prompt("test command", default=detected)

    # -- sandbox
    if sandbox == "docker" and not hw.docker:
        console.print("[yellow]docker not found; using local sandbox[/yellow]")
        sandbox = "local"

    # -- git: guild needs a repo for branches/merge
    if hw.git and not (root / ".git").is_dir():
        do_init = yes or typer.confirm(
            "This folder is not a git repository. Initialise one? (needed for branches)",
            default=True,
        )
        if do_init:
            import subprocess

            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=False)
            if (root / ".git").is_dir():
                console.print("[green]initialised git repository (branch main)[/green]")
                _ensure_gitignore(root)
                # guild branches from HEAD; an unborn HEAD means no branches and no commits
                subprocess.run(["git", "add", "-A"], cwd=root, check=False)
                r = subprocess.run(
                    ["git", "commit", "-q", "-m", "initial commit (guild init)"],
                    cwd=root,
                    check=False,
                    capture_output=True,
                    encoding="utf-8",
                    errors="replace",
                )
                if r.returncode == 0:
                    console.print("[green]created initial commit[/green]")
                else:
                    console.print(
                        "[yellow]could not create the initial commit "
                        "(set git user.name/user.email, then: git add -A && git commit -m init)"
                        "[/yellow]"
                    )

    # -- write config + tailored profile
    cfg = ProjectConfig(profile=profile, test_command=test_command, sandbox=sandbox)
    p = save_project_config(root, cfg)
    for sub in ("roles", "profiles", "runs"):
        (root / GUILD_DIR / sub).mkdir(parents=True, exist_ok=True)
    prof = hwmod.build_profile(hw, profile)
    prof_path = root / GUILD_DIR / "profiles" / f"{profile}.yaml"
    prof_path.write_text(
        "# Generated by `guild init` for this machine. Edit freely; this overrides the "
        "built-in profile.\n" + yaml.safe_dump(prof, sort_keys=False),
        encoding="utf-8",
    )

    # -- project docs (PRD / architecture / rules / tasks / memory / decisions)
    if docs is None and not yes:
        docs = typer.confirm(
            "Scaffold project docs (PRD, ARCHITECTURE, RULES, TASKS, MEMORY, DECISIONS)? "
            "Every role reads them for context",
            default=not (root / "docs" / "PRD.md").exists(),
        )
    if docs:
        created = _scaffold_docs(root)
        if created:
            console.print(
                "[green]created:[/green] " + ", ".join(created) + "  — fill in PRD.md first"
            )

    _ensure_gitignore(root)

    # -- what to pull
    pulls = (
        hwmod.missing_pulls(prof["slots"], hw)
        if hw.ollama_reachable
        else [
            m.split("/", 1)[1]
            for chain in prof["slots"].values()
            for m in chain
            if m.startswith("ollama/")
        ]
    )
    pulls = list(dict.fromkeys(pulls))[:3]
    lines = [f"config:  {p}", f"profile: [bold]{profile}[/bold] → {prof_path}", ""]
    for slot, chain in prof["slots"].items():
        lines.append(
            f"  {slot:<17} {chain[0]}"
            + (f"  (then {', '.join(chain[1:3])})" if len(chain) > 1 else "")
        )
    if pulls:
        lines += ["", "Pull the local models first:"] + [f"  ollama pull {m}" for m in pulls]
    if not hw.git:
        lines += ["", "[yellow]git is not installed — guild needs it to make branches.[/yellow]"]
    lines += [
        "",
        "Next: [bold]guild doctor[/bold], then [bold]guild ui[/bold] "
        'or [bold]guild plan "..."[/bold]',
    ]
    console.print(Panel("\n".join(lines), title="guild init"))


def _scaffold_docs(root: Path) -> list[str]:
    """Copy doc templates into the project without overwriting anything that exists."""
    from importlib import resources

    tdir = resources.files("guild").joinpath("data", "templates")
    targets = {
        "PRD.md": "docs/PRD.md",
        "ARCHITECTURE.md": "docs/ARCHITECTURE.md",
        "MEMORY.md": "docs/MEMORY.md",
        "DECISIONS.md": "docs/DECISIONS.md",
        "RULES.md": "RULES.md",
        "TASKS.md": "TASKS.md",
    }
    created: list[str] = []
    for src, rel in targets.items():
        dst = root / rel
        if dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(tdir.joinpath(src).read_text(encoding="utf-8"), encoding="utf-8")
        created.append(rel)
    return created


_GITIGNORE_LINES = (
    ("# guild run traces (may contain code excerpts)", f"{GUILD_DIR}/runs/"),
    ("# caches guild's test runs create", "__pycache__/"),
    (None, "*.pyc"),
    (None, ".pytest_cache/"),
    (None, ".ruff_cache/"),
    (None, ".mypy_cache/"),
)


def _ensure_gitignore(root: Path) -> None:
    """Add the lines guild relies on (idempotent; keeps whatever is already there)."""
    gi = root / ".gitignore"
    existing = gi.read_text(encoding="utf-8") if gi.exists() else ""
    have = {ln.strip() for ln in existing.splitlines()}
    add: list[str] = []
    for comment, line in _GITIGNORE_LINES:
        if line in have or line.rstrip("/") in have:
            continue
        if comment:
            add.append(comment)
        add.append(line)
    if add:
        with open(gi, "a", encoding="utf-8") as f:
            f.write(
                ("\n" if existing and not existing.endswith("\n") else "") + "\n".join(add) + "\n"
            )


@app.command()
def doctor(
    project: Path | None = typer.Option(None, "--project", "-C"),
    profile: str | None = typer.Option(None, "--profile", "-p"),
):
    """Check which providers in the active profile are reachable / configured."""
    root, cfg, prof = _load(project, profile)
    from . import hardware as hwmod

    hw = hwmod.detect()
    console.print(f"project: {root}\nprofile: [bold]{prof.name}[/bold] — {prof.description}\n")
    console.print(Panel("\n".join(hwmod.summary_lines(hw)), title="machine"))
    t = Table(title="Slots")
    t.add_column("slot")
    t.add_column("candidate")
    t.add_column("status")
    ollama_ok = _ollama_models()
    for slot, chain in prof.slots.items():
        for i, m in enumerate(chain):
            prefix, _, name = m.partition("/")
            if prefix == "ollama":
                if ollama_ok is None:
                    status = "[red]ollama not reachable[/red]"
                elif name in ollama_ok or name.split(":")[0] in {
                    x.split(":")[0] for x in ollama_ok
                }:
                    status = "[green]ready[/green]"
                else:
                    status = f"[yellow]not pulled[/yellow] → ollama pull {name}"
            elif prefix == "anthropic":
                status = (
                    "[green]key set[/green]"
                    if os.environ.get("ANTHROPIC_API_KEY")
                    else "[dim]ANTHROPIC_API_KEY missing[/dim]"
                )
            elif prefix in ENDPOINTS:
                ep = ENDPOINTS[prefix]
                if (
                    ep.key_env is None
                    or os.environ.get(ep.key_env)
                    or prefix in {"omniroute", "litellm", "custom"}
                ):
                    status = "[green]configured[/green]"
                else:
                    status = f"[dim]{ep.key_env} missing[/dim]"
            else:
                status = "[red]unknown provider[/red]"
            t.add_row(slot if i == 0 else "", m, status)
    console.print(t)
    rec = hwmod.recommend_local(hw)
    console.print(
        "recommended local models for this machine: "
        + ", ".join(dict.fromkeys(m for c in rec.values() for m in c[:1]))
    )
    pulls = hwmod.missing_pulls(prof.slots, hw) if hw.ollama_reachable else []
    if pulls:
        console.print(
            "[yellow]not pulled yet:[/yellow] " + "  ".join(f"ollama pull {m}" for m in pulls[:4])
        )
    console.print(
        f"\nsandbox: {cfg.sandbox}"
        + (
            ""
            if cfg.sandbox != "docker" or shutil.which("docker")
            else "  [red](docker not found)[/red]"
        )
    )
    has_git = (root / ".git").is_dir()
    console.print(f"git: {'yes' if has_git else '[yellow]no — branches/commits disabled[/yellow]'}")
    console.print(f"test command: {cfg.test_command}")


def _ollama_models() -> list[str] | None:
    import httpx

    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    try:
        r = httpx.get(f"{host}/api/tags", timeout=3)
        return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return None


@app.command()
def plan(
    goal: str = typer.Argument(..., help="what you want built"),
    project: Path | None = typer.Option(None, "--project", "-C"),
    profile: str | None = typer.Option(None, "--profile", "-p"),
    context_file: Path | None = typer.Option(
        None, "--context", help="extra brief (e.g. a Kaggle competition description)"
    ),
):
    """Ask the Lead for a roadmap and task list. Saved to .guild/plan.json."""
    root, cfg, prof = _load(project, profile)
    extra = context_file.read_text(encoding="utf-8") if context_file else ""
    g = _guild(root, cfg, prof)
    try:
        p = g.plan(goal, extra_context=(f"ADDITIONAL BRIEF:\n{extra}" if extra else ""))
    except (AllProvidersFailed, BudgetExceeded, RuntimeError) as e:
        err.print(f"[red]{e}[/red]")
        g.close()
        raise typer.Exit(1) from None
    g.close()
    _show_plan(p)
    console.print(_cost_table(g.tracker))
    console.print(f"\ntrace: {g.trace.path}")


def _show_plan(p) -> None:
    console.print(
        Panel(
            "\n".join(f"{i + 1}. {m}" for i, m in enumerate(p.roadmap)) or "(none)", title="Roadmap"
        )
    )
    t = Table(title="Tasks")
    t.add_column("id")
    t.add_column("status")
    t.add_column("title")
    t.add_column("done when")
    t.add_column("deps")
    for task in p.tasks:
        t.add_row(task.id, task.status, task.title, task.done_when, ",".join(task.depends_on))
    console.print(t)
    if p.warnings():
        console.print(
            Panel("\n".join(f"- {w}" for w in p.warnings()), title="Plan check", style="yellow")
        )
    if p.risks:
        console.print(Panel("\n".join(f"- {r}" for r in p.risks), title="Risks"))
    if p.questions_for_owner:
        console.print(
            Panel(
                "\n".join(f"- {q}" for q in p.questions_for_owner),
                title="Questions for you",
                style="yellow",
            )
        )


@app.command()
def status(project: Path | None = typer.Option(None, "--project", "-C")):
    """Show the current plan and task statuses."""
    root, _cfg, _prof = _load(project, None)
    from .workflow import Plan

    p = root / GUILD_DIR / "plan.json"
    if not p.exists():
        console.print('no plan yet — run [bold]guild plan "..."[/bold]')
        raise typer.Exit()
    _show_plan(Plan.from_dict(json.loads(p.read_text(encoding="utf-8"))))


@app.command()
def run(
    task_ids: list[str] | None = typer.Argument(
        None, help="task id(s), e.g. T1 T3 (default: next runnable)"
    ),
    project: Path | None = typer.Option(None, "--project", "-C"),
    profile: str | None = typer.Option(None, "--profile", "-p"),
    all_tasks: bool = typer.Option(False, "--all", help="run every runnable task in order"),
    yes: bool = typer.Option(False, "--yes", "-y", help="don't pause between tasks"),
    skip: str = typer.Option("", help="comma-separated roles to skip, e.g. security,docs"),
):
    """Implement task(s) from the plan: engineer → verify → critic/security → lead → docs."""
    root, cfg, prof = _load(project, profile)
    g = _guild(root, cfg, prof)
    plan_ = g.load_plan()
    if plan_ is None:
        err.print("no plan — run `guild plan` first")
        raise typer.Exit(2)
    roles = [
        r for r in cfg.roles_enabled if r not in {s.strip() for s in skip.split(",") if s.strip()}
    ]

    try:
        queue_ = list(task_ids or [])
        while True:
            if queue_:
                task = plan_.get(queue_.pop(0))
                if task.status == "done":
                    continue
            else:
                task = None if task_ids else plan_.next_task()
            if task is None:
                console.print("[green]no runnable tasks left[/green]")
                break
            console.print(
                Panel(
                    f"[bold]{task.id}[/bold] {task.title}\n{task.description}\n\n"
                    f"[dim]done when:[/dim] {task.done_when}",
                    title="task",
                )
            )
            if (
                not yes
                and not all_tasks
                and not task_ids
                and not typer.confirm("Run this task?", default=True)
            ):
                break
            out = g.run_task(plan_, task, roles=roles)
            _show_outcome(out)
            if not out.accepted:
                console.print("[yellow]stopping: task not accepted[/yellow]")
                break
            if not queue_ and not all_tasks:
                break
            if (
                not yes
                and not queue_
                and not typer.confirm("Continue with next task?", default=True)
            ):
                break
    except (AllProvidersFailed, BudgetExceeded) as e:
        err.print(f"[red]{e}[/red]")
    except KeyboardInterrupt:
        err.print("[yellow]interrupted[/yellow]")
    finally:
        g.close()
    console.print(_cost_table(g.tracker))
    console.print(f"\ntrace: {g.trace.path}")


def _show_outcome(out) -> None:
    colour = "green" if out.accepted else "yellow"
    lines = [
        f"[{colour}]{'ACCEPTED' if out.accepted else 'NOT ACCEPTED'}[/{colour}]  "
        f"rounds={out.rounds}  escalated={out.escalated}  cost=${out.cost_usd:.4f}"
    ]
    if out.task.branch:
        lines.append(f"branch: {escape(out.task.branch)}   (review and merge it yourself)")
    if out.verification:
        v = out.verification
        status = "pass" if v.get("passed") else "FAIL"
        lines.append(f"tests: {status} ({v.get('tests_run', '?')} run)")
    for label, rev in (("critic", out.critic), ("security", out.security)):
        if rev:
            summary = escape(str(rev.get("summary") or "")[:200])
            lines.append(f"{label}: {rev.get('verdict')} — {summary}")
            for f in rev.get("findings", [])[:8]:
                sev, file, issue = (
                    escape(str(f.get(k) or "")) for k in ("severity", "file", "issue")
                )
                lines.append(f"   \\[{sev}] {file}: {issue}")
    if out.lead_decision:
        ld = out.lead_decision
        lines.append(f"lead: {ld.get('decision')} — {escape(str(ld.get('notes') or '')[:300])}")
        for imp in out.lead_decision.get("improvements", [])[:5]:
            lines.append(f"   ↳ suggestion: {escape(str(imp))}")
    if out.task.status == "blocked":
        lines.append(f"[red]blocked:[/red] {escape(out.task.notes)}")
    console.print(Panel("\n".join(lines), title=escape(f"{out.task.id} {out.task.title}")))


@app.command()
def ask(
    role: str = typer.Argument(..., help="lead | critic | security | researcher | ..."),
    question: str = typer.Argument(...),
    project: Path | None = typer.Option(None, "--project", "-C"),
    profile: str | None = typer.Option(None, "--profile", "-p"),
):
    """Ask one role a question about the project (read-only)."""
    root, cfg, prof = _load(project, profile)
    g = _guild(root, cfg, prof)
    try:
        res = g.ask(role, question)
    except (AllProvidersFailed, BudgetExceeded, FileNotFoundError) as e:
        err.print(f"[red]{e}[/red]")
        g.close()
        raise typer.Exit(1) from None
    g.close()
    console.print(
        Panel(
            json.dumps(res.data, indent=2) if res.data else res.raw, title=f"{role} ({res.model})"
        )
    )
    console.print(_cost_table(g.tracker))


@app.command()
def review(
    project: Path | None = typer.Option(None, "--project", "-C"),
    profile: str | None = typer.Option(None, "--profile", "-p"),
    ref: str = typer.Option("HEAD", help="diff against this ref (e.g. main)"),
    security_only: bool = typer.Option(False),
):
    """Run Critic and Security on the current diff without implementing anything."""
    root, cfg, prof = _load(project, profile)
    g = _guild(root, cfg, prof)
    diff = g._git("diff", ref) or g._git("diff") or "(no diff)"
    ctx = {"Diff": diff[:30000]}
    try:
        results = {}
        if not security_only:
            results["critic"] = g.agent("critic").run("Review the change.", context_blocks=ctx)
        results["security"] = g.agent("security").run(
            "Review the change for security and privacy.", context_blocks=ctx
        )
    except (AllProvidersFailed, BudgetExceeded) as e:
        err.print(f"[red]{e}[/red]")
        g.close()
        raise typer.Exit(1) from None
    g.close()
    for name, res in results.items():
        console.print(
            Panel(
                json.dumps(res.data, indent=2) if res.data else res.raw,
                title=f"{name} ({res.model})",
            )
        )
    console.print(_cost_table(g.tracker))


@app.command()
def cost(
    project: Path | None = typer.Option(None, "--project", "-C"),
    last: int = typer.Option(10, help="how many runs to summarise"),
):
    """Summarise token usage and estimated cost from saved traces."""
    from .trace import read_trace

    root = find_project_root(project)
    runs = sorted((root / GUILD_DIR / "runs").glob("*/trace.jsonl"))[-last:]
    if not runs:
        console.print("no runs yet")
        raise typer.Exit()
    t = Table(title=f"last {len(runs)} runs")
    t.add_column("run")
    t.add_column("profile")
    t.add_column("calls", justify="right")
    t.add_column("in tok", justify="right")
    t.add_column("out tok", justify="right")
    t.add_column("USD", justify="right")
    grand = 0.0
    for p in runs:
        ev = read_trace(p)
        prof = next((e.get("profile") for e in ev if e["kind"] == "run_start"), "?")
        calls = [e for e in ev if e["kind"] == "model_call"]
        usd = sum(e.get("usd", 0) for e in calls)
        grand += usd
        t.add_row(
            p.parent.name,
            prof,
            str(len(calls)),
            f"{sum(e['input_tokens'] for e in calls):,}",
            f"{sum(e['output_tokens'] for e in calls):,}",
            f"{usd:.4f}",
        )
    console.print(t)
    console.print(f"estimated total: ${grand:.4f}  (≈ £{grand * 0.78:.2f})")


@app.command()
def roles(project: Path | None = typer.Option(None, "--project", "-C")):
    """List available roles and profiles."""
    root = find_project_root(project)
    t = Table(title="Roles")
    t.add_column("name")
    t.add_column("slot")
    t.add_column("tools")
    t.add_column("description")
    for n in list_available("roles", root):
        r = load_role(n, root)
        t.add_row(
            r.name,
            r.slot + (f" → {r.escalation_slot}" if r.escalation_slot else ""),
            ", ".join(r.tools),
            r.description.strip(),
        )
    console.print(t)
    t2 = Table(title="Profiles")
    t2.add_column("name")
    t2.add_column("budget £/mo")
    t2.add_column("description")
    for n in list_available("profiles", root):
        p = load_profile(n, root)
        t2.add_row(
            p.name,
            "—" if p.monthly_budget_gbp is None else str(p.monthly_budget_gbp),
            p.description,
        )
    console.print(t2)


@app.command()
def watch(
    project: Path | None = typer.Option(None, "--project", "-C"),
    profile: str | None = typer.Option(None, "--profile", "-p"),
    interval: int = typer.Option(10, help="seconds between checks"),
    roles: str = typer.Option("critic,security", help="comma-separated reviewer roles"),
    once: bool = typer.Option(False, help="review the current HEAD once and exit"),
):
    """Watch the repo; every new commit gets reviewed by the Critic and Security roles."""
    import subprocess
    import time as _time

    root, cfg, prof = _load(project, profile)
    reviewers = [r.strip() for r in roles.split(",") if r.strip()]

    def head() -> str:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
        return r.stdout.strip()

    def review(sha: str) -> None:
        g = _guild(root, cfg, prof)
        diff = (
            g._git("show", "--format=%h %s%n", sha, "--", ".", f":(exclude){GUILD_DIR}")
            or "(empty)"
        )
        console.rule(f"[cyan]{sha[:8]}")
        try:
            for r in reviewers:
                res = g.agent(r).run(
                    "Review this commit.", context_blocks={"Commit diff": diff[:30000]}
                )
                d = res.data or {}
                colour = {"approve": "green", "request_changes": "yellow", "block": "red"}.get(
                    d.get("verdict"), "white"
                )
                summary = d.get("summary", res.raw[:200])
                console.print(f"[{colour}]{r}: {d.get('verdict', '?')}[/{colour}] — {summary}")
                for f in d.get("findings", [])[:8]:
                    console.print(f"   [{f.get('severity')}] {f.get('file')}: {f.get('issue')}")
        except (AllProvidersFailed, BudgetExceeded) as e:
            err.print(f"[red]{e}[/red]")
        finally:
            g.close()

    last = head()
    if not last:
        err.print("not a git repository")
        raise typer.Exit(2)
    if once:
        review(last)
        return
    console.print(
        f"watching {root} every {interval}s — Ctrl+C to stop (reviewers: {', '.join(reviewers)})"
    )
    try:
        while True:
            _time.sleep(interval)
            cur = head()
            if cur and cur != last:
                last = cur
                review(cur)
    except KeyboardInterrupt:
        console.print("stopped")


@app.command()
def ui(
    project: Path | None = typer.Option(None, "--project", "-C"),
    port: int = typer.Option(7331, help="local port"),
    no_browser: bool = typer.Option(False, "--no-browser", help="don't open a browser tab"),
):
    """Open the local web dashboard (needs `pip install guild-ai[ui]`)."""
    root = find_project_root(project)
    try:
        from .ui.server import serve
    except ImportError:
        err.print(
            "[red]dashboard needs extra packages:[/red]  "
            'pip install "guild-ai[ui]"   (or: pip install fastapi uvicorn)'
        )
        raise typer.Exit(2) from None
    serve(root, port=port, open_browser=not no_browser)


if __name__ == "__main__":
    app()
