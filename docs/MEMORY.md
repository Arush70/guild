# Project Memory — guild

Current state of the project. Update at the end of every working session.

## Current status (2026-09-27)

v0.2.x. All features built and passing 61 tests with scripted fake models; dashboard
verified in a headless browser. TASK-001 now has one accepted free-profile real-model run;
repeatability and the file-preservation issue below remain open.

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
- Validation in the Guild repo: `pytest -q` passed (61 tests); `ruff check src tests` failed
  with 92 findings. No Guild source code was changed in this session.

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

TASK-002 in `TASKS.md`: address the Engineer's failure to preserve existing files, with a
FakeProvider regression test, then rerun the Demo task. TASK-001 is complete.

## Known issues

- Unconfirmed on real models: push-backs, streaming parser on Ollama, validation/repair.
- One earlier real run crashed with `TypeError: unsupported operand type(s) for +:
  'NoneType' and 'str'` after verification; not reproduced since; tracebacks are now
  captured in the trace and dashboard if it recurs.
- Chat on the free profile uses the 7b model in the `frontier` slot; answers are shallow.
  A free Groq/Gemini key in `frontier` helps.
- Chat history is in memory only (lost on restart).
- `pyproject.toml` Homepage still points at a placeholder user; real repo is
  https://github.com/Arush70/guild.

## Owner's environment

Windows 11, RTX 4070 mobile 8 GB, 16 GB RAM. Repo `C:\Machine Learning projects\guild`,
venv `.venv` (activate: `.venv\Scripts\Activate.ps1`). Demo project
`C:\Machine Learning projects\Demo` (was empty; `git init -b main` it). Ollama models:
qwen2.5-coder:7b, qwen2.5-coder:3b, qwen3:8b, deepseek-r1:8b.

## Next step

Complete TASK-002: strengthen the prompt/parsing behavior so existing files are read and
edited without replacing unrelated code or tests; add a FakeProvider regression test and
rerun the Demo task. Then continue to TASK-003.
