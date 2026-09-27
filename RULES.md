# Development Rules — guild

These rules apply to any AI (Claude Code, guild's own agents) or human changing this repo.

## Before coding

- Read `CLAUDE.md`, `docs/PRD.md`, `docs/ARCHITECTURE.md`, `TASKS.md`, `docs/MEMORY.md`.
- Inspect the existing implementation and tests for the area you're changing.
- For anything beyond a small fix, state the plan (files, approach, how it will be tested)
  before editing.
- Prefer changing a role YAML over changing Python when the behaviour is "what the model
  should do".

## General

- Python 3.10+. `ruff check src tests` must pass (line length 100; E702 allowed only in
  `cli.py`). `pytest -q` must pass.
- Keep functions small; do not duplicate logic; do not touch unrelated files.
- No new dependencies without a line in `docs/DECISIONS.md`.
- Keep the dashboard a single `index.html` with no build step and no external scripts.

## Safety model (never weaken)

- Tools resolve inside the project root and refuse `..`.
- critic / security / verifier / assistant are read-only. docs writes `*.md` only.
- `*KEY*`, `*TOKEN*`, `*SECRET*`, `*PASSWORD*` env vars are stripped from subprocesses.
- guild never commits unless it created the branch; never merges/pushes on its own.
- `.guild/` is never committed by guild and never appears in review diffs.
- The `free` profile refuses any model whose price is > 0.
- The dashboard binds to 127.0.0.1 only.

## Small-model tolerance (never remove)

- `extract_text_tool_calls` executes tool calls written as text.
- Output contracts in `schemas.py` coerce near-misses and drive the repair loop.
- "done" with no file changes → push back, then block. First "blocked" → push back once.
- Plan check flags vague `done_when`.

## Testing

- Tests never call a real API: use `FakeProvider` (scripted turns per role) or a small
  custom provider registered with `router.register_provider("fake", obj)`.
- Every bug fixed from a real run gets a test that reproduces the model behaviour that
  caused it.
- UI changes: run the headless-browser smoke script (see `docs/TEST_PLAN.md`).

## Git

- Small commits with descriptive messages (`feat:`, `fix:`, `docs:` prefixes welcome).
- Work on `main` for now; use branches for multi-day features.
- Never run `guild plan` / `guild run` inside this repo — use a separate project folder.

## Documentation

- After a feature: update `CHANGELOG.md` (Unreleased), `docs/MEMORY.md` (state), and
  `TASKS.md` (tick / add). Update `README.md` if behaviour or commands changed.
- Record any architectural choice in `docs/DECISIONS.md`.
