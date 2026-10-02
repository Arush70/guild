# CLAUDE.md — context for Claude Code working on this repo

## Read these first, in this order (do not modify anything until you have)

1. `docs/PRD.md` — what guild is and the v1.0 success criteria
2. `docs/ARCHITECTURE.md` — modules, data flow, architectural rules
3. `RULES.md` — how to change this repo (lint/tests/safety model/small-model tolerance)
4. `TASKS.md` — what to do next, in order; one task per session
5. `docs/MEMORY.md` — current state, what the last real run showed, known issues
6. `docs/DECISIONS.md` — permanent decisions; add an ADR before changing any of them
7. `docs/TEST_PLAN.md`, `docs/SECURITY.md`, `docs/DESIGN.md` as needed

Then: state your understanding and the plan for the current TASK before writing code.
After every task: `ruff check src tests && pytest -q`, commit, tick `TASKS.md`, update
`docs/MEMORY.md` and `CHANGELOG.md`.

## What guild is

A budget-aware multi-agent coding team, run from the CLI (`guild ...`) or a local dashboard
(`guild ui`). Roles (lead, engineer, critic, security, docs, researcher) are YAML files in
`src/guild/data/roles/`; a profile (`free` / `lite` / `pro`, in `src/guild/data/profiles/`)
maps abstract model slots to a fallback chain of concrete `provider/model` ids.

Workflow per task: Engineer implements on a `guild/<task>` git branch → guild runs the test
command itself (deterministic, no LLM) → Critic + Security review the diff → Engineer revises
(bounded rounds; escalates to a stronger model after N failures) → Lead ACCEPT/REVISE/REPLAN →
Docs updates Markdown. Guild never merges to main by itself; the human clicks merge in the
dashboard (or merges with git).

Owner: Arush Kumar Vishwakarma (MSc student, University of Exeter). MIT licence. This tool
shares its JSONL trace discipline with his dissertation project (MACS) but is a separate repo.

## Layout

```
src/guild/
  cli.py              typer commands: init(wizard) doctor plan status run review ask cost roles watch ui
  workflow.py         Guild class: plan(), revise_plan(), run_task(), run_tasks(), verify(), ask(), chat()
  agent.py            one role's tool-calling loop; extract_json; extract_text_tool_calls;
                      validates the final reply against schemas.py and asks for repairs (≤2)
  schemas.py          pydantic output contract per role/job (lenient coercions for small models)
  hardware.py         detect GPU/RAM/Ollama/keys; recommend_local(); build_profile(); detect_test_command()
  config.py           Profile / Role / ProjectConfig models + loaders (.guild/ overrides win)
  trace.py            JSONL trace writer/reader (.guild/runs/<id>/trace.jsonl)
  sandbox.py          LocalSandbox / DockerSandbox; secrets stripped from env
  tools/registry.py   file/search/shell/git/web tools, scoped to project root
  providers/          openai_compat (Ollama, Groq, Gemini, DeepSeek, OpenRouter, OmniRoute,
                      LiteLLM…), anthropic_provider, router (fallback chain + cost tracking)
  ui/server.py        FastAPI + SSE hub (events + throttled token stream); switchable project (_State);
                      /api/fs folder picker, /api/files + /api/file browser, /api/chat, /api/plan/revise;
                      branch diff/merge/discard; run detail with per-role cost.  ui/index.html  single-file dashboard, no build step
  data/roles/*.yaml   lead engineer critic security docs researcher performance kaggle assistant(chat, prose)
  data/templates/*.md PRD/ARCHITECTURE/RULES/TASKS/MEMORY/DECISIONS scaffolds for `guild init --docs`
  data/profiles/*.yaml free lite pro (guild init writes a machine-specific override into .guild/profiles/)
tests/                74 tests; FakeProvider in conftest.py scripts model replies, no API calls;
                      test_robustness.py (schemas/repair/streaming), test_hardware.py (init wizard)
```

## Conventions

- Python 3.10+, `ruff check src tests && ruff format --check src tests` must pass (rule set is
  pinned in pyproject.toml so results don't drift between ruff versions; line length 100).
- `pytest` must pass. Tests never call real APIs — use `FakeProvider` (scripted turns per role)
  or a tiny custom provider class registered with `router.register_provider("fake", obj)`.
- Keep the safety model intact: tools resolve inside the project root and refuse `..`;
  critic/security/verifier are read-only; docs may only write `*.md`; `*KEY*`/`*TOKEN*`/
  `*SECRET*` env vars are stripped from subprocesses; `.guild/` is never committed by guild
  and never appears in review diffs; the `free` profile refuses any model with a price > 0.
- Every role replies with a strict JSON contract (see each YAML). `extract_json` tolerates
  fences/prose; there is one repair retry.
- Small local models often write tool calls as text — `extract_text_tool_calls` executes them.
  Keep this working; it's essential for 7b models on Ollama.
- Plan writes are atomic (`os.replace`). The UI reads plan.json concurrently; `/api/state`
  snapshots job status BEFORE reading files (ordering matters — it was a race).
- Branch safety: `_commit` is a no-op unless `_start_branch` succeeded; dirty = modified
  tracked files only; pre-existing untracked files are excluded from guild's commits.
- Providers accept `on_token` for streaming; Router passes it through; Hub.token() coalesces.
- `Guild.project_docs()` feeds a project's PRD/ARCHITECTURE/RULES/TASKS/MEMORY/DECISIONS/DESIGN
  (when present) into every role's context via `project_summary()`. Lead/engineer/docs prompts
  say to obey them and keep MEMORY/TASKS updated.
- Add features as new roles/profiles (YAML) before adding code. New OpenAI-compatible
  providers are one line in `providers/openai_compat.py::ENDPOINTS`.

## Owner's hardware and setup

Windows laptop, i9-14900HX, 16 GB RAM, RTX 4070 mobile (8 GB VRAM). Ollama installed.
Realistic local models: `qwen2.5-coder:7b` (engineer), `deepseek-r1:8b` (critic/security),
`qwen3:8b`. 27b+ models do not fit; don't recommend them. Free cloud keys (Groq, Gemini) are
the cheap way to get a stronger Lead. The owner can spend ~£10–20/month on API keys (`lite`).

Dev environment: repo at `C:\Machine Learning projects\guild`, venv at `.venv`
(`.venv\Scripts\Activate.ps1`), installed with `pip install -e ".[dev]"`. Demo project for
manual testing at `C:\Machine Learning projects\demo` (git branch is `master`).

## State of the project (2026-10-02, v0.2.x)

Works end to end with a scripted fake provider (74 tests) and verified in a headless browser
(streaming, plan check, diff modal, trace viewer, merge). One real run on the free profile
(Ollama `qwen2.5-coder:7b`) ACCEPTED the demo task (TASK-001) but overwrote existing code;
the read-before-write / removed-tests guards (TASK-002) and a full code audit (see
CHANGELOG "Unreleased") landed afterwards and have NOT yet been confirmed on a real run.

First thing to do: re-run the demo (`cd ..\Demo && guild ui`, goal "Add a multiply(a, b)
function to app.py and a test_multiply test in test_app.py") with the guards in place, read
`.guild/runs/<latest>/trace.jsonl`, then TASK-003 (calculator plan, run all).

## Roadmap (owner's priorities)

1. **Get one task accepted end-to-end on the free profile with local models** (run the demo,
   read the trace, tune prompts). Then the same on `lite`. Then on 2 more small projects.
2. Publish to PyPI as `guild-ai` (wheel builds clean: `python -m build`), tag v1.0.0 once (1) holds.
3. Parallel engineers on independent tasks (per-task git worktrees).
4. `ux` / `data-quality` reviewer roles; wire extra reviewers from `roles_enabled` into run_task.
5. Optional CI job running the loop against a real Ollama.
6. VS Code extension (the dashboard HTTP API is the integration point).
Done in 0.2.0: streaming, trace viewer, diff/merge, init wizard, output validation, kaggle +
performance roles, `guild watch`, GitHub Action, troubleshooting docs.

## Suggested first prompt for a new Claude Code session

> Read CLAUDE.md, then docs/PRD.md, docs/ARCHITECTURE.md, RULES.md, TASKS.md and
> docs/MEMORY.md. Do not modify anything yet. Summarise the product, the architecture, the
> rules, and the current state in your own words, list anything missing or contradictory,
> and explain your implementation plan for the first unticked task in TASKS.md. Then wait.

## Commands you will use

```
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
ruff check src tests && ruff format --check src tests && pytest -q
guild --help
guild doctor -C ..\demo
guild plan "..." -C ..\demo
guild run -C ..\demo
guild ui -C ..\demo --no-browser
```
