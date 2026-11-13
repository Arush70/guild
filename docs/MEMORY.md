# Project Memory — codeguild

Current state of the project. Update at the end of every working session.

## Current status (2026-10-02)

v0.3.0 (renamed to codeguild). 386 tests with scripted fake models (193 at the time of this entry); `ruff check` + `ruff format --check` clean (rule set
pinned). TASK-001 has one accepted free-profile real-model run; TASK-002 guards are in. A full
code audit on 2026-10-02 fixed 14 defects (see CHANGELOG "Unreleased") — none of it is confirmed
on a real model run yet; that re-run is the next step.

## 0.3.0, 2026-11-13: renamed to codeguild, IDE dashboard

- **Rename** (ADR-021): everything is `codeguild` — pip name, import, command, `.codeguild/`,
  `codeguild/<task>` branches, VS Code ids (`codeguild.*`). `.guild/` projects are migrated on
  first use (`paths.adopt_legacy_project`). The GitHub repository is still `Arush70/guild`.
- **Dashboard** (ADR-022) rewritten as an IDE around a vendored Monaco: explorer, editor tabs
  with save, task changes as per-file before/after, approvals as diffs, bottom panel, team in
  the status bar. New endpoints: `POST /api/file`, `/api/branches/{id}/files` and `/file`.
  Checked in headless Chrome (walkthrough, a full plan → run → diff → pair approval against the
  scripted model, and a markup-injection sweep); those scripts are not in the repository.
- **Trust** is per folder now, and `codeguild init` trusts what it sets up; the first version
  asked owners to re-approve their own committed config.
- **First Windows CI run** (run #20): pytest green; two extension e2e tests failed. One was a
  real bug: new files written by `write_file` got CRLF on Windows (text-mode newline
  translation); fixed and pinned in `test_hardening3.py`. The other compared a path against
  Windows' 8.3 short name of the same folder (test only).
- **PyPI**: not uploaded. `python -m build` and `twine check` pass; the wheel is about 6 MB
  because of Monaco. Before uploading: a green Windows CI run, one real-model task.

## Break-it audit 2, 2026-11-12 (after the untangle refactor)

Attacked: command policy, tools and approval gate, sandbox, git, stored files, dashboard API
and page, providers, verification. All found with running code; each has a test in
`tests/test_hardening3.py`. What was wrong:

- **Inspection allowlist** (read-only roles; runs without approval in pair mode) was bypassed
  by quoting (`"--output=x"`), variables, short options (`sort -o`, `ruff check -o`), and
  options that write, execute or upload (`pytest --log-file`, `pylint --init-hook`,
  `mypy --install-types`, `npm audit fix`, `pytest --pastebin`). `cat .env` and
  `cat /etc/passwd` were allowed. Rewritten in `tools/policy.py`: shell-aware splitting,
  unsafe-option table, path arguments checked against the project and the credential rule.
- **Approval gate**: `run_command` with a list, `run_tests` with `; cmd` in `extra_args`,
  `write_file` with non-text content and overwriting a non-UTF-8 file all ran without being
  shown. Now: previews fail closed; ESC and bidi characters are made visible.
- **Budget**: one model reply with 200 tool calls ran all 200. Checked per call now.
- **Spending cap**: a hosted model missing from the price table counted as $0 under any cap.
- **Sandbox**: commands inherited the terminal's stdin, output was unbounded in memory
  (876 MB for a 300 MB flood), a background child held the pipe until the timeout, stale
  `.pyc` could make a test run the previous version of a file.
- **Verification**: skipping/xfailing every test, or forcing exit code 0 in a conftest hook,
  passed broken code.
- **Project root**: after the dashboard had written `~/.codeguild/recent.json`, any folder under
  home without `.git` resolved to the *home directory* as the project.
- **Dashboard**: XSS through a role file name and through numbers in a trace file; a broken
  `config.yaml` made every page load a 500; another localhost port counted as same-origin;
  cancel during job start-up hit the previous job; an HTML error page from a gateway in
  stream mode was an "empty reply" instead of a failed call.
- **No lock**: two codeguild processes (terminal dashboard + the extension's) could run jobs on
  one folder at once. `ProjectLock` (ADR-018).
- Also: Windows aliases of `.git`/`.env` (`.GIT`, `.git.`, `GIT~1`, `.env::$DATA`), UNC paths,
  a new `.env` committed/reviewed, plan.json values of the wrong type, dependency circles.

Third pass (2026-11-13), closing what the audit had left open:

- `search` matches in a worker process (`tools/_regex_worker.py`) that is killed after 20 s;
  a pattern like `(a+)+$` used to hang the agent thread for good.
- A repository whose `.codeguild` configuration is *tracked by git* must be approved once with
  `codeguild trust` before any job runs (`trust.py`, ADR-020); a change to those files asks again.
- `web_fetch` connects to the address it vetted (Host header and TLS name stay the host
  name), so a second DNS answer cannot redirect it to localhost.
- Anthropic model ids and prices updated from the models page (fable-5-2, opus-5-5,
  sonnet-5-5, haiku-5-5; older ids stay priced). Every model a profile suggests has a price.
- Event-stream readers are bounded (a stalled tab is disconnected and reconnects); the
  extension no longer drops an event that shares a timestamp with the last one seen; a
  failed commit is written to the trace again.
- CI runs on Windows too. Until a Windows run is green, the Windows-only code (process
  lock via `msvcrt`, `taskkill`, cmd.exe quoting) has still only been reasoned about.

Left as it is: text tool calls. A model that *writes* a tool call as JSON has it executed;
that gives a prompt-injected model nothing it could not do with a native call, and small
local models depend on it. A zip download of a hostile project (no git) is not covered by
the trust check.

## Untangle refactor 2026-11-12

Whole-codebase restructuring with no intended behaviour change; the full decision log is
`docs/UNTANGLE.md`. `workflow.py` went from ~1300 to ~750 lines: the plan (`plan.py`), git
(`repo.py`), verification (`verify.py`), `.codeguild` paths (`paths.py`), path scoping
(`tools/context.py`) and command policy (`tools/policy.py`) are modules of their own, and the
CLI and dashboard use them instead of private `Guild` methods or raw dicts. 229 Python tests,
22 extension tests. Not yet run on Windows or with a real model — do that before trusting it.
The Anthropic model ids in the profiles and price table should be checked against the
provider's current list.

## Break-it audit 2026-11-12 (what was wrong and what changed)

Full list in CHANGELOG "Unreleased". The ones that change how the next real run behaves:

- Push-back after "blocked" (the exact path the first real run took): the files the engineer
  then created were never committed, so the branch and the review diff lacked them. Fixed.
- Ollama streams every tool call of a turn with index 0; codeguild glued them into one broken call.
  Fixed (split on a new call id). Text tool calls whose file content contains a lone `{`/`}` or
  raw newlines are parsed now.
- Free profile + a Groq/Gemini key: those models have a list price, so the $0 rule has always
  rejected them — but only after sending the request. They are skipped before the call now.
  To actually use a free tier on `free`, set the model to 0 in `.codeguild/prices.yaml`
  (`groq/llama-3.3-70b-versatile: 0`). OPEN DECISION for the owner: should `codeguild init` write
  that file when it adds free-tier models to the free profile? (Not done: RULES says the $0
  rule is never weakened without a decision.)
- Ctrl+C no longer strands a task in `in_progress`.
- New trace event `review_input` (round, diff_chars, files_in_diff): what the reviewers saw.
- `Task.base` records the branch a task started from; the dashboard merges back into it.
- None of this is confirmed on a real model run yet (same status as the 2026-10-02 audit).
- Not verified here: Windows-only paths (taskkill, cp1252) and real provider APIs — the
  provider fixes are tested against fakes that mimic the documented error/stream shapes.

## Pair mode (2026-11-12)

`codeguild pair` / `/api/pair` / pair modes in the dashboard and VS Code chat (TASK-030, ADR-016).
Verified with scripted models only, including the extension e2e test (approve, decline, open
diff, cancel a running command). NOT yet tried on a real model. Expect the free profile's 7b
model to be weak here; the role prompt is `data/roles/pair.yaml`. Known limit: between turns
only the text of the conversation is replayed, not tool results, so the agent re-reads files
each turn (TASK-031). SSE: an approval travels as `{"kind": "approval_request", "request":
{id, kind: edit|command, path|command, preview, proposed}}`; `/api/state.pending_approvals`
has the same objects for clients that connect late.

## VS Code extension (2026-11-12)

`vscode-extension/` v0.1.0 exists (TASK-040, ADR-014). It is a client of `codeguild ui`; any API
change in `ui/server.py` must keep `vscode-extension/test/e2e.test.js` green. The e2e test also
proves the Python side against an Ollama-shaped stream (all tool calls on index 0). Not yet
done: a manual click-through in real VS Code on Windows. On the owner's machine `codeguild.command`
must point at `C:\Machine Learning projects\guild\.venv\Scripts\codeguild.exe` unless that venv
is on PATH. Owner decided the web dashboard may vendor Monaco for an IDE layout (ADR-015,
TASK-041, not started).

## Audit 2026-10-02 (what was wrong and what changed)

- The "ruff 92 findings" were default-rule drift between ruff versions, not code problems; the
  rule set is now pinned in `pyproject.toml` and the code is `ruff format`ted. Keep both green.
- Windows: pytest/git output is decoded as UTF-8 with replacement (cp1252 crashed on `—`);
  a hung test run is killed as a tree (`taskkill /F /T`) instead of hanging codeguild.
- Dashboard merge wrote `task.merged` into plan.json, which `Task(**t)` could not load → every
  later run/chat/status died. `Task.merged` exists now and unknown keys are ignored.
- Verification created `__pycache__`/`.pytest_cache` *after* `_pre_untracked` was captured, so
  they were committed; after a merge they were tracked-and-modified → "dirty" → no more
  branches. Excluded via pathspecs; `codeguild init` gitignores them.
- Critic/security replies that never parsed (3 tries) were `None` and counted as approval.
- Branching: rejected tasks go back to the start branch; accepted ones stay (so run-all
  stacks dependent tasks); merge refuses self-merge; no branch on an unborn HEAD.
- Read-only roles enforce an allowlist of inspection commands (`Role.readonly: true`).
- Schemas coerce null/number/object string fields; `<think>` blocks are stripped before JSON
  extraction; dangling `depends_on` ids are dropped.

## Latest real run (2026-09-27, Ollama qwen2.5-coder:7b, free profile)

- TASK-001 T1 was ACCEPTED in one round on branch `codeguild/t1-add-multiply-function-and-test`.
- Trace: `C:\Machine Learning projects\Demo\.codeguild\runs\20260927-171904-486bcc\trace.jsonl`.
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
- Folder had no git repo (Doctor `git: no`) → no branches. → `codeguild init` now offers
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
`codeguild/t1-add-multiply-function-and-test` branch in Demo first — it deleted `add`), then the
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
