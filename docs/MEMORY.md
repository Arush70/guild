# Project Memory — guild

Current state of the project. Update at the end of every working session.

## Current status (2026-10-02)

v0.2.x. 74 tests with scripted fake models; `ruff check` + `ruff format --check` clean (rule set
pinned). TASK-001 has one accepted free-profile real-model run; TASK-002 guards are in. A full
code audit on 2026-10-02 fixed 14 defects (see CHANGELOG "Unreleased") — none of it is confirmed
on a real model run yet; that re-run is the next step.

## Audit 2026-10-02 (what was wrong and what changed)

- The "ruff 92 findings" were default-rule drift between ruff versions, not code problems; the
  rule set is now pinned in `pyproject.toml` and the code is `ruff format`ted. Keep both green.
- Windows: pytest/git output is decoded as UTF-8 with replacement (cp1252 crashed on `—`);
  a hung test run is killed as a tree (`taskkill /F /T`) instead of hanging guild.
- Dashboard merge wrote `task.merged` into plan.json, which `Task(**t)` could not load → every
  later run/chat/status died. `Task.merged` exists now and unknown keys are ignored.
- Verification created `__pycache__`/`.pytest_cache` *after* `_pre_untracked` was captured, so
  they were committed; after a merge they were tracked-and-modified → "dirty" → no more
  branches. Excluded via pathspecs; `guild init` gitignores them.
- Critic/security replies that never parsed (3 tries) were `None` and counted as approval.
- Branching: rejected tasks go back to the start branch; accepted ones stay (so run-all
  stacks dependent tasks); merge refuses self-merge; no branch on an unborn HEAD.
- Read-only roles enforce an allowlist of inspection commands (`Role.readonly: true`).
- Schemas coerce null/number/object string fields; `<think>` blocks are stripped before JSON
  extraction; dangling `depends_on` ids are dropped.

## Latest real run (2026-09-27, Ollama qwen2.5-coder:7b, free profile)

- TASK-001 T1 was ACCEPTED in one round on branch `guild/t1-add-multiply-function-and-test`.
- Trace: `C:\Machine Learning projects\Demo\.guild\runs\20260927-171904-486bcc\trace.jsonl`.
- The Engineer emitted three tool calls as text; Guild executed two successful `write_file`
  calls for `app.py` and `test_app.py`, followed by `run_tests` (2 passed). Deterministic
  verification passed; critic, security, and lead approved. Run cost was $0.00.
- The Demo was not empty: it already had `add` and `test_add`. The Engineer did not read
  either file and overwrote them, removing that existing behavior/test while adding multiply.
  T1 met its generated completion condition but exposed a prompt weakness: the existing
  read-before-write guidance did not prevent full rewrites. Preserve existing content in
  future prompt/parsing work and add a FakeProvider regression test before treating this
  behavior as resolved.
- The `docs` role's first model candidate, `qwen2.5-coder:3b`, was unavailable; fallback
  allowed the run to complete. This did not prevent acceptance.
- Validation in the Guild repo: `pytest -q` passed (61 tests); `ruff check src tests` reported
  92 findings (resolved 2026-10-02: ruff default-rule drift; rule set now pinned).

## What the last real run showed (Ollama qwen2.5-coder:7b, free profile, empty Demo folder)

- Lead produced a good 4-task calculator plan (concrete signatures, testable done_when). ✔
- Doctor: all local models ready. Chat answered (generic). ✔
- Engineer got "no such file: app.py" and reported **blocked** instead of creating it. ✘
  → fixed: task brief lists missing files; first "blocked" is pushed back once.
- `run selected` ran T3 before T2 was done. ✘ → fixed: unfinished dependencies are skipped.
- Folder had no git repo (Doctor `git: no`) → no branches. → `guild init` now offers
  `git init`; dashboard shows a warning banner.
- pytest on an empty project returned exit 5 ("no tests collected") and counted as failure.
  → fixed.
- These fixes are NOT yet confirmed on a real run.

## Completed

- Core loop, roles, profiles, router with fallback + cost cap, JSONL traces, sandboxes
- Deterministic verification; text tool-call execution; output contracts + repair
- Dashboard: plan/run/streaming/diff/merge/trace/cost, project picker, files, chat, revise
- Init wizard with hardware detection; doctor; watch; GitHub Action; kaggle/performance roles
- Docs: README, TROUBLESHOOTING, ADDING_A_ROLE, PRD, ARCHITECTURE, DESIGN, DECISIONS,
  TEST_PLAN, SECURITY, RULES, TASKS, this file

## Current task

TASK-003 in `TASKS.md`. First re-run the Demo T1 with the TASK-002 guards (discard the old
`guild/t1-add-multiply-function-and-test` branch in Demo first — it deleted `add`), then the
4-task calculator plan with run all.

## Known issues

- Unconfirmed on real models: push-backs, streaming parser on Ollama, validation/repair.
- One earlier real run crashed with `TypeError: unsupported operand type(s) for +:
  'NoneType' and 'str'` after verification; not reproduced since; tracebacks are now
  captured in the trace and dashboard if it recurs.
- Chat on the free profile uses the 7b model in the `frontier` slot; answers are shallow.
  A free Groq/Gemini key in `frontier` helps.
- Chat history is in memory only (lost on restart).

## Owner's environment

Windows 11, RTX 4070 mobile 8 GB, 16 GB RAM. Repo `C:\Machine Learning projects\guild`,
venv `.venv` (activate: `.venv\Scripts\Activate.ps1`). Demo project
`C:\Machine Learning projects\Demo` (was empty; `git init -b main` it). Ollama models:
qwen2.5-coder:7b, qwen2.5-coder:3b, qwen3:8b, deepseek-r1:8b.

## Next step

Re-run the Demo on `free` with the guards and audit fixes in place; read the trace; confirm the
engineer reads `app.py` before rewriting it and that `test_add` survives. Then TASK-003.
