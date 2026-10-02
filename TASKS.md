# Tasks — guild

Work top to bottom. One task per Claude Code session. Each task: implement → `pytest` →
review the diff → commit → tick here and update `docs/MEMORY.md`.

## Phase 1 — Prove the loop on real local models (v1.0 gate)

- [x] TASK-001 Run the demo on `free` (`cd ..\Demo`, `guild ui`, goal "Add a multiply(a, b)
      function to app.py and a test_multiply test in test_app.py"). Read
      `.guild/runs/<latest>/trace.jsonl`. Record what the 7b Engineer did in `docs/MEMORY.md`.
- [x] TASK-002 Fix whatever TASK-001 exposed (prompt or parsing, not architecture). Add a
      test reproducing the model behaviour. (Guards + tests landed 2026-10-02; the re-run of
      T1 on the real model with them is the first step of TASK-003.)
- [ ] TASK-003 Run the 4-task calculator plan end to end with **run all**; every task accepted
      and merged. Note total wall time and tokens.
- [ ] TASK-004 Repeat TASK-003 three times from a fresh folder; it must succeed 3/3.
- [ ] TASK-005 Same demo on `lite` with `ANTHROPIC_API_KEY` set. Confirm cost stays under
      `max_usd_per_run` and `guild cost` matches the provider dashboard within 20%.
- [ ] TASK-006 Chat quality: ask 5 questions about the demo project; the Assistant must read
      files before answering (check trace for `list_files`/`read_file`). Tune the prompt if not.

## Phase 2 — Release

- [ ] TASK-010 Check the PyPI name `guild-ai` is free (the `guild` name is taken by Guild AI).
      If not, rename the package (candidates: `guild-agents`, `codeguild`) — update
      `pyproject.toml`, README, CLAUDE.md, `console_scripts` stays `guild`.
- [ ] TASK-011 (passes on Linux as of 2026-10-02; confirm on Windows) `python -m build`, install the wheel in a clean venv, run `guild init -y` and
      `guild doctor` in a temp folder. Fix any missing package data.
- [ ] TASK-012 Publish `0.3.0` to TestPyPI, then PyPI. Tag `v1.0.0` once Phase 1 is 3/3.
- [ ] TASK-013 README: replace the screenshot with one from a real accepted run; add a 30-second
      GIF of plan → run → merge.

## Phase 3 — Product depth

- [ ] TASK-020 `guild init --docs`: scaffold `docs/PRD.md`, `docs/ARCHITECTURE.md`, `RULES.md`,
      `TASKS.md`, `docs/MEMORY.md` templates in a project (done in 0.2.x — verify on Windows).
- [ ] TASK-021 Wire extra reviewers from `roles_enabled` (e.g. `performance`) into `run_task`.
- [ ] TASK-022 Per-task git worktrees so two engineers can run in parallel on independent tasks.
- [ ] TASK-023 `ux` and `data-quality` reviewer roles.
- [ ] TASK-024 Optional CI job that runs the loop against a real Ollama (nightly, non-blocking).
- [ ] TASK-025 Persist chat history to `.guild/chat.jsonl` so it survives restarts.

## Done

- [x] 0.1.0 core loop, roles, profiles, CLI, traces, sandbox
- [x] 0.2.0 dashboard, streaming, validation/repair, init wizard, kaggle/performance, watch
- [x] 0.2.x project picker, files, chat, revise plan, push-backs, dependency gating, git init offer
