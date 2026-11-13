"""codeguild — command line."""

from __future__ import annotations

import functools
import json
import shutil
import time
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from . import __version__, projectdocs
from . import hardware as hwmod
from .config import (
    ConfigError,
    ProjectConfig,
    find_project_root,
    list_available,
    load_profile,
    load_project_config,
    load_role,
    save_project_config,
)
from .errors import Cancelled
from .paths import CACHE_DIRS, ProjectBusy, ProjectPaths
from .plan import Plan, PlanError, PlanStore, Task
from .providers import AllProvidersFailed, BudgetExceeded
from .providers.status import ollama_models, profile_status
from .repo import Repo
from .trace import read_trace, summarize
from .trust import needs_trust, shipped_config
from .trust import trust as trust_project
from .workflow import PAIR_MODES, REVIEWERS, Guild, PairSession, TaskOutcome

app = typer.Typer(
    help="A budget-aware multi-agent coding team.", no_args_is_help=True, rich_markup_mode="rich"
)
console = Console()
err = Console(stderr=True)


def command(fn):
    """Register a CLI command. A config, profile, role or plan file that cannot be used ends
    the command with one line naming the file, not a traceback."""

    @functools.wraps(fn)
    def run(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ConfigError, PlanError, ProjectBusy) as e:
            err.print(f"[red]{escape(str(e))}[/red]")
            raise typer.Exit(2) from None

    return app.command()(run)


def _report(level: str, msg: str) -> None:
    style = {"phase": "bold cyan", "warn": "yellow", "fallback": "yellow", "info": "dim"}.get(
        level,
        "dim",  # an empty style would print "[]…[/]" and raise a rich MarkupError
    )
    prefix = {"phase": "▶", "warn": "!", "fallback": "!"}.get(level, "·")
    console.print(f"[{style}]{prefix} {escape(msg)}[/{style}]")  # model text may contain [..]


def _reason(e: Exception) -> str:
    """One line for the user; a pydantic error otherwise prints a paragraph and a URL."""
    errors = getattr(e, "errors", None)
    return errors()[0]["msg"] if callable(errors) else str(e)


def _load(project: Path | None, profile_override: str | None):
    root = find_project_root(project)
    try:
        cfg = load_project_config(root)
        if profile_override:
            cfg.profile = profile_override
        profile = load_profile(cfg.profile, root)
    except (FileNotFoundError, ValueError) as e:  # ValueError: invalid YAML / invalid values
        err.print(f"[red]{escape(_reason(e))}[/red]")
        raise typer.Exit(2) from None
    return root, cfg, profile


def _guild(root, cfg, profile, **kw) -> Guild:
    return Guild(root, cfg, profile, report=_report, **kw)


def _load_plan(root: Path) -> Plan | None:
    try:
        return PlanStore(ProjectPaths(root).plan).load()
    except PlanError as e:
        err.print(f"[red]{escape(str(e))}[/red]")
        raise typer.Exit(2) from None


def _review_lines(name: str, review: dict | None, fallback: str = "") -> list[str]:
    """A reviewer's verdict, summary and first findings. All of it is model text: escaped so
    "[high]" or "[/x]" is shown as written instead of being read as rich markup."""
    review = review or {}
    colour = {"approve": "green", "request_changes": "yellow", "block": "red"}.get(
        review.get("verdict"), "white"
    )
    summary = escape(str(review.get("summary") or fallback)[:200])
    lines = [f"[{colour}]{name}: {review.get('verdict', '?')}[/{colour}] — {summary}"]
    for f in review.get("findings", [])[:8]:
        sev, file, issue = (escape(str(f.get(k) or "")) for k in ("severity", "file", "issue"))
        lines.append(f"   \\[{sev}] {file}: {issue}")
    return lines


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
        console.print(f"codeguild {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def _main(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", "-V", is_eager=True, callback=_version_cb),
):
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())


def _choose_tier(hw: hwmod.Hardware, yes: bool) -> str:
    default = "lite" if hw.keys.get("anthropic") else "free"
    if yes:
        return default
    free_cloud = " + your free cloud keys" if hw.keys.get("groq") or hw.keys.get("gemini") else ""
    console.print(
        f"[bold]Which profile?[/bold]\n  [cyan]free[/cyan]  £0 — local Ollama models{free_cloud}"
        "\n  [cyan]lite[/cyan]  ~£10–20/mo — Claude plans & reviews, local model codes "
        "(needs ANTHROPIC_API_KEY)"
        "\n  [cyan]pro[/cyan]   pay-as-you-go — best model everywhere "
        "(needs Anthropic/OpenAI/Gemini keys)"
    )
    return typer.prompt("profile", default=default).strip().lower()


def _existing_config(root: Path) -> ProjectConfig:
    """Re-running init must not wipe settings edited by hand (ignore list, lint_command,
    roles_enabled, docker_image): start from the existing config when there is a valid one."""
    try:
        return load_project_config(root)
    except ValueError:
        return ProjectConfig()


def _init_repo(root: Path, yes: bool) -> None:
    """codeguild needs a repository with at least one commit: it branches from HEAD."""
    if Repo(root).exists:
        return
    if not yes and not typer.confirm(
        "This folder is not a git repository. Initialise one? (needed for branches)", default=True
    ):
        return
    repo = Repo.init(root)
    if repo is None:
        return
    console.print("[green]initialised git repository (branch main)[/green]")
    _ensure_gitignore(root)
    first = repo.commit_all("initial commit (codeguild init)")
    if first.committed:
        console.print("[green]created initial commit[/green]")
        if first.warning:
            console.print(f"[yellow]{escape(first.warning)}[/yellow]")
    else:
        console.print(
            "[yellow]could not create the initial commit "
            "(then: git add -A && git commit -m init)[/yellow]"
        )


def _init_summary(hw: hwmod.Hardware, profile: dict, config_path: Path, profile_path: Path) -> str:
    slots = profile["slots"]
    pulls = hwmod.missing_pulls(slots, hw)
    lines = [f"config:  {config_path}", f"profile: [bold]{profile['name']}[/bold] → {profile_path}"]
    lines.append("")
    for slot, chain in slots.items():
        then = f"  (then {', '.join(chain[1:3])})" if len(chain) > 1 else ""
        lines.append(f"  {slot:<17} {chain[0]}{then}")
    if pulls:
        lines += ["", "Pull the local models first:"]
        lines += [f"  ollama pull {m}" for m in pulls[:3]]
    if not hw.git:
        lines += [
            "",
            "[yellow]git is not installed — codeguild needs it to make branches.[/yellow]",
        ]
    lines += [
        "",
        "Next: [bold]codeguild doctor[/bold], then [bold]codeguild ui[/bold] "
        'or [bold]codeguild plan "..."[/bold]',
    ]
    return "\n".join(lines)


@command
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
    """Set up codeguild for a project: detects your hardware, keys and test runner, picks models
    that fit, and writes .codeguild/config.yaml plus a tailored profile. Optionally scaffolds the
    project documents every role reads for context."""
    root = path.resolve()
    root.mkdir(parents=True, exist_ok=True)
    console.print(Panel("\n".join(hwmod.summary_lines(hw := hwmod.detect())), title="your machine"))

    profile = profile or _choose_tier(hw, yes)
    try:
        prof = hwmod.build_profile(hw, profile)
        mine = not shipped_config(root)  # no config came with the repository: it is the owner's
        cfg = _existing_config(root)
        cfg.profile, cfg.sandbox = profile, sandbox
    except ValueError as e:  # unknown profile tier or sandbox kind
        err.print(f"[red]{escape(_reason(e))}[/red]")
        raise typer.Exit(2) from None
    if profile in {"lite", "pro"} and not hw.keys.get("anthropic"):
        console.print(
            "[yellow]note: ANTHROPIC_API_KEY is not set — Claude seats will fall back to other "
            "candidates until you set it.[/yellow]"
        )

    detected = hwmod.detect_test_command(root)
    if test_command is None:
        test_command = detected if yes else typer.prompt("test command", default=detected)

    if cfg.sandbox == "docker" and not hw.docker:
        console.print("[yellow]docker not found; using local sandbox[/yellow]")
        cfg.sandbox = "local"

    if hw.git:
        _init_repo(root, yes)

    cfg.test_command = test_command
    config_path = save_project_config(root, cfg)
    paths = ProjectPaths(root)
    for d in (paths.overrides("roles"), paths.overrides("profiles"), paths.runs):
        d.mkdir(parents=True, exist_ok=True)
    prof_path = paths.overrides("profiles") / f"{profile}.yaml"
    prof_path.write_text(
        "# Generated by `codeguild init` for this machine. Edit freely; this overrides the "
        "built-in profile.\n" + yaml.safe_dump(prof, sort_keys=False),
        encoding="utf-8",
    )

    if docs is None and not yes:
        docs = typer.confirm(
            "Scaffold project docs (PRD, ARCHITECTURE, RULES, TASKS, MEMORY, DECISIONS)? "
            "Every role reads them for context",
            default=not (root / "docs" / "PRD.md").exists(),
        )
    if docs:
        created = projectdocs.scaffold(root)
        if created:
            console.print(
                "[green]created:[/green] " + ", ".join(created) + "  — fill in PRD.md first"
            )

    _ensure_gitignore(root)
    if mine:
        trust_project(root)  # set up here by the owner: committing it later needs no approval
    console.print(Panel(_init_summary(hw, prof, config_path, prof_path), title="codeguild init"))


_GITIGNORE_LINES = (
    (
        "# codeguild run traces (may contain code excerpts)",
        f"{ProjectPaths(Path()).runs.as_posix()}/",
    ),
    ("# caches codeguild's test runs create", f"{CACHE_DIRS[0]}/"),
    (None, "*.pyc"),
    *((None, f"{d}/") for d in CACHE_DIRS[1:]),
)


def _ensure_gitignore(root: Path) -> None:
    """Add the lines codeguild relies on (idempotent; keeps whatever is already there)."""
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


@command
def doctor(
    project: Path | None = typer.Option(None, "--project", "-C"),
    profile: str | None = typer.Option(None, "--profile", "-p"),
):
    """Check which providers in the active profile are reachable / configured."""
    root, cfg, prof = _load(project, profile)
    hw = hwmod.detect()
    console.print(f"project: {root}\nprofile: [bold]{prof.name}[/bold] — {prof.description}\n")
    console.print(Panel("\n".join(hwmod.summary_lines(hw)), title="machine"))
    t = Table(title="Slots")
    t.add_column("slot")
    t.add_column("candidate")
    t.add_column("status")
    colour = {"ready": "green", "missing": "yellow", "broken": "red"}
    last_slot = None
    for m in profile_status(prof, ollama_models()):
        t.add_row(
            "" if m.slot == last_slot else m.slot,
            m.model,
            f"[{colour[m.level]}]{escape(m.status)}[/{colour[m.level]}]",
        )
        last_slot = m.slot
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
    has_git = Repo(root).exists
    console.print(f"git: {'yes' if has_git else '[yellow]no — branches/commits disabled[/yellow]'}")
    console.print(f"test command: {cfg.test_command}")


@command
def plan(
    goal: str = typer.Argument(..., help="what you want built"),
    project: Path | None = typer.Option(None, "--project", "-C"),
    profile: str | None = typer.Option(None, "--profile", "-p"),
    context_file: Path | None = typer.Option(
        None, "--context", help="extra brief (e.g. a Kaggle competition description)"
    ),
):
    """Ask the Lead for a roadmap and task list. Saved to .codeguild/plan.json."""
    root, cfg, prof = _load(project, profile)
    extra = context_file.read_text(encoding="utf-8") if context_file else ""
    g = _guild(root, cfg, prof)
    try:
        p = g.plan(goal, extra_context=(f"ADDITIONAL BRIEF:\n{extra}" if extra else ""))
    except (AllProvidersFailed, BudgetExceeded, RuntimeError) as e:
        err.print(f"[red]{escape(str(e))}[/red]")
        raise typer.Exit(1) from None
    finally:
        g.close()
    _show_plan(p)
    console.print(_cost_table(g.tracker))
    console.print(f"\ntrace: {g.trace.path}")


def _show_plan(p: Plan) -> None:
    console.print(
        Panel(
            "\n".join(f"{i + 1}. {escape(str(m))}" for i, m in enumerate(p.roadmap)) or "(none)",
            title="Roadmap",
        )
    )
    t = Table(title="Tasks")
    t.add_column("id")
    t.add_column("status")
    t.add_column("title")
    t.add_column("done when")
    t.add_column("deps")
    for task in p.tasks:
        t.add_row(
            *(
                escape(str(c))
                for c in (
                    task.id,
                    task.status,
                    task.title,
                    task.done_when,
                    ",".join(task.depends_on),
                )
            )
        )
    console.print(t)
    if p.warnings():
        console.print(
            Panel(
                "\n".join(f"- {escape(w)}" for w in p.warnings()),
                title="Plan check",
                style="yellow",
            )
        )
    if p.risks:
        console.print(Panel("\n".join(f"- {escape(str(r))}" for r in p.risks), title="Risks"))
    if p.questions_for_owner:
        console.print(
            Panel(
                "\n".join(f"- {escape(str(q))}" for q in p.questions_for_owner),
                title="Questions for you",
                style="yellow",
            )
        )


@command
def status(project: Path | None = typer.Option(None, "--project", "-C")):
    """Show the current plan and task statuses."""
    root, _cfg, _prof = _load(project, None)
    plan_ = _load_plan(root)
    if plan_ is None:
        console.print('no plan yet — run [bold]codeguild plan "..."[/bold]')
        raise typer.Exit()
    _show_plan(plan_)


@command
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
    plan_ = _load_plan(root)
    if plan_ is None:
        err.print("no plan — run `codeguild plan` first")
        raise typer.Exit(2)
    try:
        plan_.require(*(task_ids or []))
    except PlanError as e:
        err.print(f"[red]{escape(str(e))}[/red]")
        raise typer.Exit(2) from None
    skipped = {s.strip() for s in skip.split(",")}
    ask_first = not (yes or all_tasks or task_ids)

    offered: list[str] = []

    def before(task: Task) -> bool:
        offered.append(task.id)
        console.print(
            Panel(
                f"[bold]{escape(task.id)}[/bold] {escape(task.title)}\n"
                f"{escape(task.description)}\n\n"
                f"[dim]done when:[/dim] {escape(task.done_when)}",
                title="task",
            )
        )
        return not ask_first or typer.confirm("Run this task?", default=True)

    def after(out: TaskOutcome) -> bool:
        _show_outcome(out)
        if not out.accepted:
            console.print("[yellow]stopping: task not accepted[/yellow]")
            return False
        more = all_tasks and plan_.next_task() is not None
        return not more or yes or typer.confirm("Continue with next task?", default=True)

    g = _guild(root, cfg, prof)
    try:
        outcomes = g.run_tasks(
            plan_,
            task_ids,
            all_tasks=all_tasks,
            roles=[r for r in cfg.roles_enabled if r not in skipped],
            stop_on_failure=True,
            before=before,
            after=after,
        )
        ran_out = all_tasks and outcomes and outcomes[-1].accepted and not plan_.next_task()
        if not offered or ran_out:
            console.print("[green]no runnable tasks left[/green]")
    except (AllProvidersFailed, BudgetExceeded) as e:
        err.print(f"[red]{escape(str(e))}[/red]")
    except KeyboardInterrupt:
        err.print("[yellow]interrupted — the task is back to 'todo'; run it again[/yellow]")
    finally:
        g.close()
    console.print(_cost_table(g.tracker))
    console.print(f"\ntrace: {g.trace.path}")


def _show_outcome(out: TaskOutcome) -> None:
    colour = "green" if out.accepted else "yellow"
    lines = [
        f"[{colour}]{'ACCEPTED' if out.accepted else 'NOT ACCEPTED'}[/{colour}]  "
        f"rounds={out.rounds}  escalated={out.escalated}  cost=${out.cost_usd:.4f}"
    ]
    if out.task.branch:
        lines.append(f"branch: {escape(out.task.branch)}   (review and merge it yourself)")
    if out.verification:
        v = out.verification
        lines.append(f"tests: {'pass' if v.passed else 'FAIL'} ({v.tests_run} run)")
    for role, review in out.reviews.items():
        lines += _review_lines(role, review)
    if out.lead_decision:
        ld = out.lead_decision
        lines.append(f"lead: {ld.get('decision')} — {escape(str(ld.get('notes') or '')[:300])}")
        for imp in out.lead_decision.get("improvements", [])[:5]:
            lines.append(f"   ↳ suggestion: {escape(str(imp))}")
    if out.task.status == "blocked":
        lines.append(f"[red]blocked:[/red] {escape(out.task.notes)}")
    console.print(Panel("\n".join(lines), title=escape(f"{out.task.id} {out.task.title}")))


@command
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
        err.print(f"[red]{escape(str(e))}[/red]")
        raise typer.Exit(1) from None
    finally:
        g.close()
    console.print(
        Panel(
            escape(json.dumps(res.data, indent=2) if res.data else res.raw),
            title=f"{role} ({res.model})",
        )
    )
    console.print(_cost_table(g.tracker))


_PAIR_HELP = (
    "[dim]/mode ask | auto-edits | plan   change what needs your approval\n"
    "/clear                          forget the conversation\n"
    "/exit                           leave (also Ctrl+D)\n"
    "Ctrl+C                          cancel the current turn[/dim]"
)


@command
def pair(
    message: str | None = typer.Argument(None, help="first request (omit to just start chatting)"),
    project: Path | None = typer.Option(None, "--project", "-C"),
    profile: str | None = typer.Option(None, "--profile", "-p"),
    mode: str = typer.Option(
        "ask", "--mode", "-m", help="ask (approve everything) | auto-edits | plan (read-only)"
    ),
    once: bool = typer.Option(False, "--once", help="answer the first request and exit"),
):
    """Pair-program in the terminal: chat with an agent that reads, edits and runs commands in
    your working tree, asking before every change. No branch, no commit: you are the reviewer."""
    try:
        session = PairSession(mode)
    except ValueError as e:
        err.print(f"[red]{escape(str(e))}[/red]")
        raise typer.Exit(2) from None
    root, cfg, prof = _load(project, profile)
    streamed = {"any": False}

    def on_token(role: str, chunk: str) -> None:
        streamed["any"] = True
        console.print(chunk, end="", markup=False, highlight=False, soft_wrap=True)

    g = _guild(root, cfg, prof, on_token=on_token)

    def ask_owner(req: dict) -> tuple[bool, str]:
        console.print()
        if req.get("kind") == "edit":
            title = f"{'create' if req.get('new_file') else 'edit'} {req.get('path')}"
            console.print(Panel(Syntax(req.get("preview") or "", "diff"), title=escape(title)))
            choices = "\\[y]es / \\[n]o / \\[a]ll edits this session"
        else:
            console.print(Panel(escape(str(req.get("command"))), title="run command"))
            choices = "\\[y]es / \\[n]o"
        try:
            answer = console.input(f"[bold]apply?[/bold] {choices} › ").strip().lower()
        except EOFError:
            answer = "n"
        if answer in ("a", "all") and req.get("kind") == "edit":
            session.all_edits = True
            return True, ""
        if answer in ("y", "yes"):
            return True, ""
        return False, "" if answer in ("n", "no", "") else answer  # anything else is feedback

    console.print(
        f"[bold]codeguild pair[/bold] · {escape(str(root))} · profile {escape(prof.name)} · "
        f"mode {session.mode}   [dim](/help for commands)[/dim]"
    )
    history: list[dict] = []
    pending = message
    try:
        while True:
            if pending is None:
                try:
                    text = console.input("\n[bold cyan]you ›[/bold cyan] ").strip()
                except EOFError:
                    break
            else:
                text, pending = pending.strip(), None
            if not text:
                continue
            if text in ("/exit", "/quit"):
                break
            if text == "/help":
                console.print(_PAIR_HELP)
                continue
            if text == "/clear":
                history.clear()
                console.print("[dim]conversation cleared[/dim]")
                continue
            if text.startswith("/mode"):
                if text[5:].strip() in PAIR_MODES:
                    session.mode = text[5:].strip()
                console.print(f"[dim]mode: {session.mode} (choices: {', '.join(PAIR_MODES)})[/dim]")
                continue
            streamed["any"] = False
            try:
                res = g.pair(text, history, session=session, ask=ask_owner)
            except (KeyboardInterrupt, Cancelled):
                console.print("\n[yellow]turn cancelled[/yellow]")
                continue
            except (AllProvidersFailed, BudgetExceeded) as e:
                err.print(f"[red]{escape(str(e))}[/red]")
                if once:
                    raise typer.Exit(1) from None
                continue
            if streamed["any"]:
                console.print()
            else:
                console.print(res.raw, markup=False, highlight=False)
            files = res.data["files_changed"]
            if files:
                console.print(f"[dim]changed: {escape(', '.join(files))}[/dim]")
            history += [
                {"role": "user", "content": text},
                {"role": "assistant", "content": res.raw},
            ]
            if once:
                break
    finally:
        g.close()
    console.print(_cost_table(g.tracker))
    console.print(f"\ntrace: {g.trace.path}")


@command
def review(
    project: Path | None = typer.Option(None, "--project", "-C"),
    profile: str | None = typer.Option(None, "--profile", "-p"),
    ref: str = typer.Option("HEAD", help="diff against this ref (e.g. main)"),
    security_only: bool = typer.Option(False),
):
    """Run Critic and Security on the current diff without implementing anything."""
    root, cfg, prof = _load(project, profile)
    diff = None if ref.startswith("-") else Repo(root).diff(ref)
    if diff is None:
        err.print(f"[red]cannot diff against {escape(ref)}: not a branch, tag or commit[/red]")
        raise typer.Exit(2)
    reviewers = ["security"] if security_only else list(REVIEWERS)
    g = _guild(root, cfg, prof)
    try:
        results = {
            r: g.agent(r).run(
                REVIEWERS[r][1], context_blocks={"Diff": (diff or "(no diff)")[:30000]}
            )
            for r in reviewers
        }
    except (AllProvidersFailed, BudgetExceeded) as e:
        err.print(f"[red]{escape(str(e))}[/red]")
        raise typer.Exit(1) from None
    finally:
        g.close()
    for name, res in results.items():
        console.print(
            Panel(
                escape(json.dumps(res.data, indent=2) if res.data else res.raw),
                title=f"{name} ({res.model})",
            )
        )
    console.print(_cost_table(g.tracker))


@command
def cost(
    project: Path | None = typer.Option(None, "--project", "-C"),
    last: int = typer.Option(10, help="how many runs to summarise"),
):
    """Summarise token usage and estimated cost from saved traces."""
    runs = list(reversed(ProjectPaths(find_project_root(project)).traces()[:last]))
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
        totals = summarize(read_trace(p))
        grand += totals["usd"]
        t.add_row(
            p.parent.name,
            totals["profile"] or "?",
            str(totals["calls"]),
            f"{totals['input_tokens']:,}",
            f"{totals['output_tokens']:,}",
            f"{totals['usd']:.4f}",
        )
    console.print(t)
    console.print(f"estimated total: ${grand:.4f}  (≈ £{grand * 0.78:.2f})")


@command
def trust(
    project: Path | None = typer.Option(None, "--project", "-C"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Trust without asking."),
):
    """Approve the codeguild configuration that came with this repository."""
    root = find_project_root(project)
    files = needs_trust(root)
    if not files:
        console.print("nothing to approve: no codeguild configuration came with this repository")
        return
    cfg = load_project_config(root)
    console.print(
        Panel(
            "\n".join(
                [
                    "These files came with the repository and decide what codeguild does here:",
                    *(f"  {escape(f)}" for f in files),
                    "",
                    f"test command:  {escape(cfg.test_command)}",
                    f"lint command:  {escape(cfg.lint_command or '(none)')}",
                    f"sandbox:       {escape(cfg.sandbox)}",
                    f"profile:       {escape(cfg.profile)}",
                    "",
                    "codeguild runs these commands on this computer. Read the files first.",
                ]
            ),
            title=escape(str(root)),
        )
    )
    if not yes and not typer.confirm("Trust this configuration?", default=False):
        raise typer.Exit(1)
    trust_project(root)
    console.print("[green]trusted[/green]")


@command
def roles(project: Path | None = typer.Option(None, "--project", "-C")):
    """List available roles and profiles."""
    root = find_project_root(project)
    t = Table(title="Roles")
    t.add_column("name")
    t.add_column("slot")
    t.add_column("tools")
    t.add_column("description")
    for n in list_available("roles", root):
        try:
            r = load_role(n, root)
        except ConfigError as e:
            t.add_row(escape(n), "", "", f"[red]{escape(str(e))}[/red]")
            continue
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
        try:
            p = load_profile(n, root)
        except ConfigError as e:
            t2.add_row(escape(n), "", f"[red]{escape(str(e))}[/red]")
            continue
        t2.add_row(
            p.name,
            "—" if p.monthly_budget_gbp is None else str(p.monthly_budget_gbp),
            p.description,
        )
    console.print(t2)


@command
def watch(
    project: Path | None = typer.Option(None, "--project", "-C"),
    profile: str | None = typer.Option(None, "--profile", "-p"),
    interval: int = typer.Option(10, help="seconds between checks"),
    roles: str = typer.Option("critic,security", help="comma-separated reviewer roles"),
    once: bool = typer.Option(False, help="review the current HEAD once and exit"),
):
    """Watch the repo; every new commit gets reviewed by the Critic and Security roles."""
    root, cfg, prof = _load(project, profile)
    reviewers = [r.strip() for r in roles.split(",") if r.strip()]
    repo = Repo(root)

    def review(sha: str) -> None:
        diff = repo.show(sha) or "(empty)"
        console.rule(f"[cyan]{sha[:8]}")
        g = _guild(root, cfg, prof)
        try:
            for r in reviewers:
                res = g.agent(r).run(
                    "Review this commit.", context_blocks={"Commit diff": diff[:30000]}
                )
                for line in _review_lines(r, res.data, fallback=res.raw):
                    console.print(line)
        except (AllProvidersFailed, BudgetExceeded) as e:
            err.print(f"[red]{escape(str(e))}[/red]")
        finally:
            g.close()

    last = repo.head()
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
            time.sleep(interval)
            cur = repo.head()
            if cur and cur != last:
                last = cur
                review(cur)
    except KeyboardInterrupt:
        console.print("stopped")


@command
def ui(
    project: Path | None = typer.Option(None, "--project", "-C"),
    port: int = typer.Option(7331, help="local port"),
    no_browser: bool = typer.Option(False, "--no-browser", help="don't open a browser tab"),
):
    """Open the local web dashboard (needs `pip install codeguild[ui]`)."""
    root = find_project_root(project)
    try:
        from .ui.server import serve
    except ImportError:
        err.print(
            "[red]dashboard needs extra packages:[/red]  "
            'pip install "codeguild[ui]"   (or: pip install fastapi uvicorn)'
        )
        raise typer.Exit(2) from None
    serve(root, port=port, open_browser=not no_browser)


if __name__ == "__main__":
    app()
