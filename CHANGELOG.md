# Changelog

## 0.2.0 — 2026-09-27
- Output contracts: every role's reply is validated (pydantic) and repaired with the error fed back (≤2 retries).
- Streaming: model output streams live into the dashboard (OpenAI-compatible and Anthropic).
- Dashboard: per-run trace viewer (timeline + per-role cost), per-task diff view with merge/discard.
- Branch safety: codeguild never commits unless on a branch it created; untracked files tolerated and kept out of its commits.
- `codeguild init` wizard: detects GPU/RAM/Ollama/keys/test runner and writes a profile that fits the machine.
- `codeguild doctor` shows machine summary, recommended models and missing pulls.
- New roles: `kaggle` (competition strategist) and `performance` (reviewer).
- `codeguild watch`: review every new commit; `docs/github-action.yml` for PR reviews in CI.
- docs: TROUBLESHOOTING.md, ADDING_A_ROLE.md.

## Unreleased
- Audit (2026-10-02): ruff rule set pinned in `pyproject.toml` + `ruff format` adopted (CI checks
  both) — newer ruff versions had been reporting ~90 "findings" that were just default-rule drift.
- Windows: subprocess output decoded as UTF-8 (cp1252 crashed on `—`); test/command timeouts
  kill the whole process tree (`taskkill /T`) instead of hanging codeguild.
- Dashboard: `merge` no longer corrupts `plan.json` for later loads (`Task.merged`; unknown keys
  ignored); switching project keeps the live event stream; double-click cannot start two jobs.
- Branch safety: `__pycache__`/`.pytest_cache`/`*.pyc` are never committed; rejected tasks return
  to the branch they started from; accepted tasks stay on theirs so dependent tasks stack; merge
  refuses a self-merge and reports git errors; no branching on a repo without commits (`codeguild init`
  now makes the initial commit and gitignores caches).
- Reviews: a critic/security reply that never parsed is "request_changes", not silent approval.
- Read-only roles (critic, security, verifier, assistant, performance) can only run an allowlist of
  inspection commands (`Role.readonly`); the old deny-regex let `echo x > f` / `sed -i` through.
- Small-model tolerance: null/number/object values in string fields are coerced; `<think>` blocks
  are stripped before JSON extraction and the last balanced object wins; dangling `depends_on` ids
  are dropped; `_norm` no longer strips the dot off `.github/...` paths.
- Local model endpoints get a 15-minute timeout (`GUILD_MODEL_TIMEOUT`); rich markup in model text
  cannot crash the CLI; docs role may edit root-level `*.md`.
- Packaging: SPDX licence metadata; wheel verified in a clean venv (`codeguild init -y`, `codeguild doctor`).
- TASK-002: `write_file` on an existing, unread file is refused (read first, then edit or rewrite keeping everything). Files written earlier in the same task are exempt.
- Verification now collects pytest node ids before the engineer starts; any pre-existing test that disappears fails verification with its name (non-pytest runners: test count must not drop).
- Project docs as context: PRD/ARCHITECTURE/RULES/TASKS/MEMORY/DECISIONS/DESIGN (also CLAUDE.md, .cursorrules) are read into every role's context when present; `codeguild init --docs` scaffolds them; Docs role keeps MEMORY/TASKS current.
- This repo now has its own docs set (docs/PRD, ARCHITECTURE, DESIGN, DECISIONS, MEMORY, TEST_PLAN, SECURITY; RULES.md; TASKS.md) for Claude Code handoff.
- Engineer: a first "blocked" is pushed back once with the facts (missing files must be created; failing tests are the work); only a second block counts. Task brief lists files that don't exist yet.
- `run selected` skips tasks whose dependencies aren't done, instead of running them out of order.
- Verification: pytest "no tests collected" (exit 5) is not a failure.
- `codeguild init` offers to `git init`; dashboard shows a warning when the project has no git repo.
- Assistant always inspects files before answering.
- Dashboard: project picker (browse folders / recent projects, switch live), Files panel with viewer, Chat with the read-only Assistant role (streaming, conversation memory), and "revise plan" by prompting.
- New role: `assistant`. New workflow methods: `Guild.chat()`, `Guild.revise_plan()`.
- Engineer that reports done without changing any file is asked again, then blocked (never falsely accepted).
- Engineer prompt: code in the reply is discarded; tools are the only way to change files.
- Crashes inside a task write an `error` event with traceback to the trace and the dashboard.
- Streaming parser hardened for Ollama quirks (missing tool-call index, reasoning-only chunks).
- Verification is deterministic: codeguild runs the test command itself (no LLM verifier in the loop).
- Text tool-call fallback: `{"name": ..., "arguments": ...}` written as plain text by small models is executed.
- `.codeguild/` is never committed or included in review diffs.
- `edit_file` rejects empty `old_text` with a helpful message.
- Selected tasks all run; only dependents of a failed task are skipped.
- Lead prompt: tasks must be concrete code changes with checkable `done_when`; questions only when blocking.
- Engineer prompt: implement a minimal version instead of blocking on vague tasks.
- Plan check: warns about tasks whose `done_when` is not verifiable (CLI + dashboard).
- Run several tasks at once: `codeguild run T1 T3`, dashboard checkboxes + **run selected**.
- `codeguild ui`: local web dashboard with live agent activity, plan editing, doctor and cost panels (`pip install codeguild[ui]`).
- Atomic plan.json writes; `.gitignore` edits no longer count as a dirty tree.

## 0.1.0 — 2026-09-20
- First release: lead / engineer / verifier / critic / security / docs / researcher roles,
  free / lite / pro profiles, fallback router with cost tracking, JSONL traces,
  local and Docker sandboxes, CLI (`init doctor plan status run review ask cost roles`).
- Break-it audit (2026-11-12) — 101 new tests in `tests/test_hardening.py` and
  `tests/test_hardening_io.py`. Safety:
  - Read-only command allowlist could be bypassed with a single `&`, a newline, `find -delete` /
    `-exec`, `git branch -D`, `sort -o`, `uniq IN OUT`, `--output=`, `rg --pre`, `ruff --add-noqa`.
  - `git_diff` passed `ref` straight to git (`--output=<path>` wrote anywhere on disk).
  - `write_file` / `edit_file` could write into `.git/` (hooks run on the host) and `.codeguild/`
    (config, roles, plan). Both are refused now; the Docker sandbox mounts them read-only.
  - An agent could call any registered tool, not only the ones in its role's `tools:` list.
  - `read_file` / `search` no longer return `.env` files; `search` no longer follows a symlink
    out of the project; `web_fetch` refuses localhost, private and metadata addresses.
  - Dashboard: requests for a non-localhost `Host` and cross-site POSTs are refused (DNS
    rebinding / CSRF); `esc()` now escapes quotes (task ids and file names sit in attributes).
  - A typo in `sandbox:` silently meant "local"; it is a validation error now.
  - Free profile: priced models were refused *after* the request had been sent. Checked before
    the call now. `.codeguild/prices.yaml` (documented, never implemented) overrides list prices.
- Workflow:
  - Files created after a "blocked" push-back were never committed (missing from the branch and
    from the reviewers' diff). New untracked files were also invisible to reviewers whenever
    codeguild had not created a branch.
  - `git commit` failures were silent; without `user.name`/`user.email` an accepted task had an
    empty branch. Falls back to a `codeguild` identity and warns on any commit failure.
  - A model-written task id like `Task 1: setup` produced an invalid branch name and crashed the
    task. Ids are normalised and de-duplicated in the plan schema.
  - Ctrl+C left the task `in_progress` forever (never picked again). Interrupted tasks go back
    to `todo`; a stale `in_progress` is runnable.
  - Deleting a task left dangling `depends_on` → `KeyError` in run-selected and a dependant
    that never ran. A Lead REVISE/REPLAN lost its notes. `revise_plan` dropped `merged`.
  - Dashboard merge always went into main/master; it now merges into the branch the task
    started from (`Task.base`). Discard only deletes `codeguild/` branches.
  - A git error message could be handed to reviewers as "the diff".
  - `limits.max_tool_calls_per_task` was never read; it now sets the Engineer's tool budget.
- Small-model tolerance: text tool calls with a brace inside file content (or raw newlines in
  JSON strings) were dropped; tool calls written inside `<think>` were executed; string numbers
  and extra keys in tool arguments failed the call; streamed tool calls sharing index 0 (Ollama,
  Gemini) were merged into one; `depends_on: [1]` and "accepted"/"approve" failed validation.
- Providers: tool results now directly follow the tool calls when the budget runs out (OpenAI
  and Anthropic reject the old order); the Anthropic JSON-repair turn no longer sends tool
  blocks without tools or empty text; a 400 naming `max_tokens` / `temperature` /
  `stream_options` is adapted and retried instead of killing the model for the run.
- `edit_file` / overwrite keep a file's line endings (LF or CRLF) instead of rewriting the file.
- Dashboard/CLI robustness: reading a trace that is being written no longer 500s; plan edits
  are validated and refused while a job holds the plan; `/api/config` validates; `codeguild run
  NOPE`, an invalid config and model text containing rich markup no longer end in a traceback;
  `codeguild init` keeps hand-edited config fields.
- VS Code extension (`vscode-extension/`, v0.1.0): Guild side bar with the plan as a task tree
  (run, review changes in VS Code's diff, merge, discard, edit), a Chat & Activity view with
  live streaming, a status bar item, doctor and run-trace views. It starts `codeguild ui` itself and
  uses the dashboard API; plain JavaScript, no dependencies. Tested end to end against a real
  codeguild server and a scripted model (`npm test`), not yet inside a real VS Code window.
- Pair mode (interactive, ADR-016): `codeguild pair` terminal session, `/api/pair`, the `pair` role.
  The agent edits the working tree and runs commands; every file change (shown as a diff) and
  every non-inspection command waits for the owner's approval. Modes: ask / auto-edits / plan.
  Approval UI in the dashboard (mode selector + dialog) and the VS Code extension (card in the
  chat view, notification, proposal opened in the diff editor).
- Cancel: `POST /api/cancel`, a *stop* button in the dashboard, "Stop Current Job" in VS Code,
  Ctrl+C in `codeguild pair`. Stops mid-generation, between tool calls, kills a running command.
- Chat history persists across restarts (`.codeguild/chat.jsonl`).
- VS Code extension 0.2.0: pair modes in the chat view, approvals, real Stop.
- Untangle refactor (2026-11-12, `docs/UNTANGLE.md`): no intended behaviour change. New modules
  `plan.py`, `repo.py`, `verify.py`, `paths.py`, `projectdocs.py`, `errors.py`,
  `providers/status.py`, `tools/context.py`, `tools/policy.py`; `run_tasks` is the only task
  loop (the CLI uses it); the dashboard server works on `Plan`/`Repo` objects, no longer on raw
  dicts or a half-constructed `Guild`. The few visible differences are listed in that file
  ("What still looks different"). 229 tests.
- Break-it audit 2 (2026-11-12; details in docs/MEMORY.md, tests in `tests/test_hardening3.py`).
  Security: inspection-command allowlist rewritten (quoting, variables, write/exec/upload
  options, credential files and paths outside the project); approval gate fails closed and
  covers `run_tests` arguments, list commands and non-text content; approval text shows hidden
  characters; more credential files and environment variables protected, also under their
  Windows aliases; dashboard XSS (role names, trace numbers) and same-origin rule (port
  included); extension declared unsupported in untrusted workspaces (0.2.1).
  Behaviour: **a hosted model without a known price is refused under a spending cap** (add
  it to `.codeguild/prices.yaml`); one codeguild process per project (`.codeguild/lock`); tool budget is
  per call; verification fails when no pre-existing test ran or failures are reported with
  exit code 0; sandbox commands get no stdin, bounded output, no `.pyc`; a folder under home
  is no longer mistaken for a project because of `~/.codeguild`; broken config/plan/role files give
  one line (CLI exit 2, dashboard still loads); empty or malformed model streams fall back
  to the next model. 366 tests.
- Third pass (2026-11-13): `search` cannot hang on a slow regex (worker process, 20 s);
  **`codeguild trust`** — a repository that brings its own tracked `.codeguild` configuration must be
  approved before codeguild runs anything in it; `web_fetch` pins the vetted address; Anthropic
  model ids/prices updated (profiles now name claude-fable-5-2, opus-5-5, sonnet-5-5,
  haiku-5-5); bounded event-stream backlog; CI on Windows. 373 tests.

## 0.3.0 (2026-11-13)
- **Renamed to codeguild.** `pip install codeguild`, command `codeguild`, import `codeguild`,
  project folder `.codeguild/`, branches `codeguild/<task>`. The old names clashed with the
  unrelated `guildai` package. Existing projects are carried over automatically (`.guild/` is
  renamed on first use). VS Code extension ids and settings are `codeguild.*`.
- **New dashboard**: an IDE-style page with the Monaco editor included in the package (works
  offline). Files can be edited and saved; a task's changes are shown file by file, before
  and after; pair-mode changes are approved from a diff; activity and chat sit in a bottom
  panel. The old single-page dashboard is gone.
- `codeguild trust` is per folder, and projects set up with `codeguild init` are trusted.
- Fix (Windows): a file an agent *created* was written with CRLF line endings whatever the
  model wrote, because Python's text mode translates `\n` there. New files are now written
  exactly as given; existing files keep their own line endings, as before. Found by the first
  Windows CI run.
- CI runs on Windows too (pytest and the VS Code extension tests), on pushes to `main` and on
  pull requests.

