"""guild — command line."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .config import (GUILD_DIR, ProjectConfig, find_project_root, list_available, load_profile,
                     load_project_config, load_role, save_project_config)
from .providers import AllProvidersFailed, BudgetExceeded
from .providers.openai_compat import ENDPOINTS

app = typer.Typer(help="A budget-aware multi-agent coding team.", no_args_is_help=True,
                  rich_markup_mode="rich")
console = Console()
err = Console(stderr=True)


def _report(level: str, msg: str) -> None:
    style = {"phase": "bold cyan", "warn": "yellow", "info": "dim", "error": "bold red"}.get(level, "")
    prefix = {"phase": "▶", "warn": "!", "info": "·", "error": "✖"}.get(level, "")
    console.print(f"[{style}]{prefix} {msg}[/{style}]")


def _load(project: Optional[Path], profile_override: Optional[str]):
    root = find_project_root(project)
    cfg = load_project_config(root)
    if profile_override:
        cfg.profile = profile_override
    try:
        profile = load_profile(cfg.profile, root)
    except FileNotFoundError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(2)
    return root, cfg, profile


def _guild(root, cfg, profile, dry_run: bool = False):
    from .workflow import Guild
    return Guild(root, cfg, profile, report=_report, dry_run=dry_run)


def _cost_table(tracker) -> Table:
    t = Table(title="Cost", show_lines=False)
    t.add_column("role"); t.add_column("calls", justify="right"); t.add_column("in tok", justify="right")
    t.add_column("out tok", justify="right"); t.add_column("USD", justify="right")
    for role, (usage, usd, n) in sorted(tracker.by_role().items()):
        t.add_row(role, str(n), f"{usage.input_tokens:,}", f"{usage.output_tokens:,}", f"{usd:.4f}")
    u = tracker.total_usage
    t.add_row("[bold]total", f"[bold]{len(tracker.calls)}", f"[bold]{u.input_tokens:,}",
              f"[bold]{u.output_tokens:,}", f"[bold]{tracker.total_usd:.4f}")
    return t


# ----------------------------------------------------------------------------- commands

def _version_cb(value: bool):
    if value:
        console.print(f"guild {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def _main(ctx: typer.Context,
          version: bool = typer.Option(False, "--version", "-V", is_eager=True, callback=_version_cb)):
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())


@app.command()
def init(
    path: Path = typer.Argument(Path("."), help="project directory"),
    profile: str = typer.Option("free", "--profile", "-p", help="free | lite | pro"),
    test_command: str = typer.Option("python -m pytest -q", help="how to run tests"),
    sandbox: str = typer.Option("local", help="local | docker"),
):
    """Create .guild/ in a project with a config and editable copies of roles/profiles."""
    root = path.resolve()
    root.mkdir(parents=True, exist_ok=True)
    cfg = ProjectConfig(profile=profile, test_command=test_command, sandbox=sandbox)
    p = save_project_config(root, cfg)
    (root / GUILD_DIR / "roles").mkdir(exist_ok=True)
    (root / GUILD_DIR / "profiles").mkdir(exist_ok=True)
    (root / GUILD_DIR / "runs").mkdir(exist_ok=True)
    gi = root / ".gitignore"
    line = f"{GUILD_DIR}/runs/\n"
    if not gi.exists() or line.strip() not in gi.read_text(encoding="utf-8"):
        with open(gi, "a", encoding="utf-8") as f:
            f.write(("\n" if gi.exists() else "") + "# guild run traces (may contain code excerpts)\n" + line)
    console.print(Panel(f"Initialised [bold]{root}[/bold]\nconfig: {p}\nprofile: [bold]{profile}[/bold]\n\n"
                        f"Override a role or profile by copying it into {GUILD_DIR}/roles/ or {GUILD_DIR}/profiles/.\n"
                        f"Next: [bold]guild doctor[/bold] then [bold]guild plan \"your goal\"[/bold]", title="guild init"))


@app.command()
def doctor(project: Optional[Path] = typer.Option(None, "--project", "-C"),
           profile: Optional[str] = typer.Option(None, "--profile", "-p")):
    """Check which providers in the active profile are reachable / configured."""
    root, cfg, prof = _load(project, profile)
    console.print(f"project: {root}\nprofile: [bold]{prof.name}[/bold] — {prof.description}\n")
    t = Table(title="Slots")
    t.add_column("slot"); t.add_column("candidate"); t.add_column("status")
    ollama_ok = _ollama_models()
    for slot, chain in prof.slots.items():
        for i, m in enumerate(chain):
            prefix, _, name = m.partition("/")
            if prefix == "ollama":
                if ollama_ok is None:
                    status = "[red]ollama not reachable[/red]"
                elif name in ollama_ok or name.split(":")[0] in {x.split(":")[0] for x in ollama_ok}:
                    status = "[green]ready[/green]"
                else:
                    status = f"[yellow]not pulled[/yellow] → ollama pull {name}"
            elif prefix == "anthropic":
                status = "[green]key set[/green]" if os.environ.get("ANTHROPIC_API_KEY") else "[dim]ANTHROPIC_API_KEY missing[/dim]"
            elif prefix in ENDPOINTS:
                ep = ENDPOINTS[prefix]
                if ep.key_env is None or os.environ.get(ep.key_env) or prefix in {"omniroute", "litellm", "custom"}:
                    status = "[green]configured[/green]"
                else:
                    status = f"[dim]{ep.key_env} missing[/dim]"
            else:
                status = "[red]unknown provider[/red]"
            t.add_row(slot if i == 0 else "", m, status)
    console.print(t)
    console.print(f"\nsandbox: {cfg.sandbox}" + ("" if cfg.sandbox != "docker" or shutil.which("docker") else "  [red](docker not found)[/red]"))
    console.print(f"git: {'yes' if (root / '.git').is_dir() else '[yellow]no — branches/commits disabled[/yellow]'}")
    console.print(f"test command: {cfg.test_command}")


def _ollama_models() -> list[str] | None:
    import httpx
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    try:
        r = httpx.get(f"{host}/api/tags", timeout=3)
        return [m["name"] for m in r.json().get("models", [])]
    except Exception:  # noqa: BLE001
        return None


@app.command()
def plan(goal: str = typer.Argument(..., help="what you want built"),
         project: Optional[Path] = typer.Option(None, "--project", "-C"),
         profile: Optional[str] = typer.Option(None, "--profile", "-p"),
         context_file: Optional[Path] = typer.Option(None, "--context", help="extra brief (e.g. a Kaggle competition description)")):
    """Ask the Lead for a roadmap and task list. Saved to .guild/plan.json."""
    root, cfg, prof = _load(project, profile)
    extra = context_file.read_text(encoding="utf-8") if context_file else ""
    g = _guild(root, cfg, prof)
    try:
        p = g.plan(goal, extra_context=(f"ADDITIONAL BRIEF:\n{extra}" if extra else ""))
    except (AllProvidersFailed, BudgetExceeded, RuntimeError) as e:
        err.print(f"[red]{e}[/red]"); g.close(); raise typer.Exit(1)
    g.close()
    _show_plan(p)
    console.print(_cost_table(g.tracker))
    console.print(f"\ntrace: {g.trace.path}")


def _show_plan(p) -> None:
    console.print(Panel("\n".join(f"{i+1}. {m}" for i, m in enumerate(p.roadmap)) or "(none)", title="Roadmap"))
    t = Table(title="Tasks")
    t.add_column("id"); t.add_column("status"); t.add_column("title"); t.add_column("done when"); t.add_column("deps")
    for task in p.tasks:
        t.add_row(task.id, task.status, task.title, task.done_when, ",".join(task.depends_on))
    console.print(t)
    if p.risks:
        console.print(Panel("\n".join(f"- {r}" for r in p.risks), title="Risks"))
    if p.questions_for_owner:
        console.print(Panel("\n".join(f"- {q}" for q in p.questions_for_owner), title="Questions for you", style="yellow"))


@app.command()
def status(project: Optional[Path] = typer.Option(None, "--project", "-C")):
    """Show the current plan and task statuses."""
    root, cfg, prof = _load(project, None)
    from .workflow import Plan
    p = root / GUILD_DIR / "plan.json"
    if not p.exists():
        console.print("no plan yet — run [bold]guild plan \"...\"[/bold]"); raise typer.Exit()
    _show_plan(Plan.from_dict(json.loads(p.read_text(encoding="utf-8"))))


@app.command()
def run(task_id: Optional[str] = typer.Argument(None, help="task id (default: next runnable)"),
        project: Optional[Path] = typer.Option(None, "--project", "-C"),
        profile: Optional[str] = typer.Option(None, "--profile", "-p"),
        all_tasks: bool = typer.Option(False, "--all", help="run every runnable task in order"),
        yes: bool = typer.Option(False, "--yes", "-y", help="don't pause between tasks"),
        skip: str = typer.Option("", help="comma-separated roles to skip, e.g. security,docs")):
    """Implement task(s) from the plan: engineer → verify → critic/security → lead → docs."""
    root, cfg, prof = _load(project, profile)
    g = _guild(root, cfg, prof)
    plan_ = g.load_plan()
    if plan_ is None:
        err.print("no plan — run `guild plan` first"); raise typer.Exit(2)
    roles = [r for r in cfg.roles_enabled if r not in {s.strip() for s in skip.split(",") if s.strip()}]

    try:
        while True:
            task = plan_.get(task_id) if task_id else plan_.next_task()
            if task is None:
                console.print("[green]no runnable tasks left[/green]"); break
            console.print(Panel(f"[bold]{task.id}[/bold] {task.title}\n{task.description}\n\n[dim]done when:[/dim] {task.done_when}",
                                title="task"))
            if not yes and not all_tasks and task_id is None:
                if not typer.confirm("Run this task?", default=True):
                    break
            out = g.run_task(plan_, task, roles=roles)
            _show_outcome(out)
            if task_id or not all_tasks:
                break
            if not out.accepted:
                console.print("[yellow]stopping: task not accepted[/yellow]"); break
            if not yes and not typer.confirm("Continue with next task?", default=True):
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
    lines = [f"[{colour}]{'ACCEPTED' if out.accepted else 'NOT ACCEPTED'}[/{colour}]  rounds={out.rounds}  "
             f"escalated={out.escalated}  cost=${out.cost_usd:.4f}"]
    if out.task.branch:
        lines.append(f"branch: {out.task.branch}   (review and merge it yourself)")
    if out.verification:
        lines.append(f"tests: {'pass' if out.verification.get('passed') else 'FAIL'} ({out.verification.get('tests_run', '?')} run)")
    for label, rev in (("critic", out.critic), ("security", out.security)):
        if rev:
            lines.append(f"{label}: {rev.get('verdict')} — {rev.get('summary', '')[:200]}")
            for f in rev.get("findings", [])[:8]:
                lines.append(f"   [{f.get('severity')}] {f.get('file')}: {f.get('issue')}")
    if out.lead_decision:
        lines.append(f"lead: {out.lead_decision.get('decision')} — {out.lead_decision.get('notes', '')[:300]}")
        for imp in out.lead_decision.get("improvements", [])[:5]:
            lines.append(f"   ↳ suggestion: {imp}")
    if out.task.status == "blocked":
        lines.append(f"[red]blocked:[/red] {out.task.notes}")
    console.print(Panel("\n".join(lines), title=f"{out.task.id} {out.task.title}"))


@app.command()
def ask(role: str = typer.Argument(..., help="lead | critic | security | researcher | ..."),
        question: str = typer.Argument(...),
        project: Optional[Path] = typer.Option(None, "--project", "-C"),
        profile: Optional[str] = typer.Option(None, "--profile", "-p")):
    """Ask one role a question about the project (read-only)."""
    root, cfg, prof = _load(project, profile)
    g = _guild(root, cfg, prof)
    try:
        res = g.ask(role, question)
    except (AllProvidersFailed, BudgetExceeded, FileNotFoundError) as e:
        err.print(f"[red]{e}[/red]"); g.close(); raise typer.Exit(1)
    g.close()
    console.print(Panel(json.dumps(res.data, indent=2) if res.data else res.raw, title=f"{role} ({res.model})"))
    console.print(_cost_table(g.tracker))


@app.command()
def review(project: Optional[Path] = typer.Option(None, "--project", "-C"),
           profile: Optional[str] = typer.Option(None, "--profile", "-p"),
           ref: str = typer.Option("HEAD", help="diff against this ref (e.g. main)"),
           security_only: bool = typer.Option(False)):
    """Run Critic and Security on the current diff without implementing anything."""
    root, cfg, prof = _load(project, profile)
    g = _guild(root, cfg, prof)
    diff = g._git("diff", ref) or g._git("diff") or "(no diff)"
    ctx = {"Diff": diff[:30000]}
    try:
        results = {}
        if not security_only:
            results["critic"] = g.agent("critic").run("Review the change.", context_blocks=ctx)
        results["security"] = g.agent("security").run("Review the change for security and privacy.", context_blocks=ctx)
    except (AllProvidersFailed, BudgetExceeded) as e:
        err.print(f"[red]{e}[/red]"); g.close(); raise typer.Exit(1)
    g.close()
    for name, res in results.items():
        console.print(Panel(json.dumps(res.data, indent=2) if res.data else res.raw, title=f"{name} ({res.model})"))
    console.print(_cost_table(g.tracker))


@app.command()
def cost(project: Optional[Path] = typer.Option(None, "--project", "-C"),
         last: int = typer.Option(10, help="how many runs to summarise")):
    """Summarise token usage and estimated cost from saved traces."""
    from .trace import read_trace
    root = find_project_root(project)
    runs = sorted((root / GUILD_DIR / "runs").glob("*/trace.jsonl"))[-last:]
    if not runs:
        console.print("no runs yet"); raise typer.Exit()
    t = Table(title=f"last {len(runs)} runs")
    t.add_column("run"); t.add_column("profile"); t.add_column("calls", justify="right")
    t.add_column("in tok", justify="right"); t.add_column("out tok", justify="right"); t.add_column("USD", justify="right")
    grand = 0.0
    for p in runs:
        ev = read_trace(p)
        prof = next((e.get("profile") for e in ev if e["kind"] == "run_start"), "?")
        calls = [e for e in ev if e["kind"] == "model_call"]
        usd = sum(e.get("usd", 0) for e in calls); grand += usd
        t.add_row(p.parent.name, prof, str(len(calls)), f"{sum(e['input_tokens'] for e in calls):,}",
                  f"{sum(e['output_tokens'] for e in calls):,}", f"{usd:.4f}")
    console.print(t)
    console.print(f"estimated total: ${grand:.4f}  (≈ £{grand * 0.78:.2f})")


@app.command()
def roles(project: Optional[Path] = typer.Option(None, "--project", "-C")):
    """List available roles and profiles."""
    root = find_project_root(project)
    t = Table(title="Roles")
    t.add_column("name"); t.add_column("slot"); t.add_column("tools"); t.add_column("description")
    for n in list_available("roles", root):
        r = load_role(n, root)
        t.add_row(r.name, r.slot + (f" → {r.escalation_slot}" if r.escalation_slot else ""),
                  ", ".join(r.tools), r.description.strip())
    console.print(t)
    t2 = Table(title="Profiles")
    t2.add_column("name"); t2.add_column("budget £/mo"); t2.add_column("description")
    for n in list_available("profiles", root):
        p = load_profile(n, root)
        t2.add_row(p.name, "—" if p.monthly_budget_gbp is None else str(p.monthly_budget_gbp), p.description)
    console.print(t2)


if __name__ == "__main__":
    app()
