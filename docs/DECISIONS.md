# Architecture Decisions — guild

Permanent decisions and why. Add an entry before changing any of these.

## ADR-001 Python, not TypeScript
Matches the owner's stack (ML, Kaggle, dissertation tooling). A VS Code extension can talk
to the dashboard's HTTP API later; it does not need the core to be TS.

## ADR-002 Roles are YAML, profiles are YAML
Behaviour lives in data so users add or tune a role without a fork, and so prompt changes
are reviewable diffs. Code is only for mechanics (tools, loop, validation).

## ADR-003 Abstract slots + fallback chains instead of fixed model names
`frontier / coder / reasoner / cheap` decouple "what quality is needed" from "which vendor".
A chain per slot gives resilience (unreachable → next) and budget control (free profile
never lists a paid model; router refuses non-zero-price models when the cap is 0).

## ADR-004 Verification is deterministic
An LLM "verifier" lied on the very first real run (it wrote `run_tests` as text and the
text got parsed as a pass). guild now runs the test command itself. A model may explain a
failure; it may not declare success.

## ADR-005 Execute tool calls written as text
7b models frequently emit `{"name": ..., "arguments": ...}` in the reply instead of using
the protocol. Refusing that makes local models useless; executing it (only for tools the
role is allowed) makes them workable. Kept even for strong models — harmless.

## ADR-006 Output contracts with lenient coercion and repair
Every role's final reply is validated (pydantic). Near-misses are coerced ("Changes
Requested" → `request_changes`); real errors are fed back verbatim for up to two repairs.
Cheaper than escalating to a bigger model, and it keeps traces clean.

## ADR-007 The human merges
guild creates `guild/<task>` branches and commits there; it never merges, pushes, or
commits on a branch it did not create. Rationale: AI-generated code must be reviewable
and reversible. Dashboard "merge" is a human click.

## ADR-008 No LLM in the loop can spend without a cap
`limits.max_usd_per_run` is enforced in the router before every call. Free profile = $0.

## ADR-009 Single-file dashboard, no build step, one process
FastAPI + one `index.html` in the CLI process. Rationale: `pip install` is the whole
install; no node, no bundler, no daemon, no database. SSE (not websockets) because it's
one-directional and trivially proxied.

## ADR-010 Traces are JSONL, one event per line, flat
Loadable with pandas; same discipline as ASTRA/MACS (the owner's dissertation). Tokens,
cost and provenance on every model call.

## ADR-011 Push back before believing a small model
"done" with no file change → push back once, then block. First "blocked" → push back once
with the facts. Selected tasks whose dependencies aren't done are skipped. Rationale: on
real runs the 7b Engineer gave up on a missing file and claimed success without writing.

## ADR-012 Optional dependencies for the UI
`fastapi`/`uvicorn` are in the `[ui]` extra so the core CLI stays light; `[dev]` includes
them so tests always cover the dashboard.

## ADR-013 Package name
`guild` on PyPI belongs to Guild AI (an unrelated ML tool). Ours is `guild-ai`; the console
command is still `guild`. If `guild-ai` turns out to be taken, rename the distribution, not
the command (see TASKS.md TASK-010).

## ADR-014 VS Code extension: a thin JavaScript client of the dashboard API
The IDE is VS Code; guild does not grow its own editor there. `vscode-extension/` starts
`guild ui --no-browser` for the opened folder and uses the same HTTP API + event stream as the
web dashboard, so there is one backend and no second implementation of the workflow (ADR-001
stays: the core is Python). The extension is plain CommonJS with no dependencies and no build
step, in the spirit of the single-file dashboard. All HTTP is done from the extension host: the
server refuses cross-origin requests, and the chat webview only exchanges messages with the
extension. Cancelling a job = restarting the server (there is no cancel endpoint yet).

## ADR-015 IDE-style web dashboard may vendor Monaco (accepted 2026-11-12, not built yet)
An IDE layout for `guild ui` (file tree, editor tabs, diff, log/chat panels) needs a real code
editor. The owner chose to vendor the Monaco editor inside the package rather than keep a plain
text area. This relaxes the RULES.md line "single `index.html` … no external scripts" to: no
build step, nothing loaded from the network, Monaco shipped as package data. Update RULES.md in
the same change that adds it (TASK-041).

## ADR-016 Pair mode: approval gate in the tool layer, no branch
Interactive pair programming (`guild pair`, `/api/pair`) is a second way to work next to the
plan → run → review pipeline, not a replacement. Decisions:
- The gate lives in `tools.registry.dispatch` (`ToolContext.approver`): before a mutating tool
  runs, `approval_request()` builds what the owner sees (unified diff + full proposed content,
  or the command) and the approver answers. It is an *additional* gate: path scoping,
  `.git`/`.guild` protection, the role's tool list and the `.env` rule all still apply, and a
  call that would be refused anyway is never shown for approval.
- Fail closed: no approver → nothing that needs approval happens.
- Inspection commands (the read-only allowlist: pytest, git status, ls …) do not ask.
- Pair works in the owner's working tree and never commits; "guild only commits on a branch it
  created" is untouched. The owner reviews live and commits themselves.
- Cancel is cooperative (`Cancelled`, a BaseException so no `except Exception` swallows it):
  checked before model calls, mid-stream, between tool calls, while a command runs and while
  an approval is pending. `POST /api/cancel` ends any job; a cancelled task returns to todo.

## ADR-017 One module per reason to change (accepted 2026-11-12)
The plan, git, verification, `.guild` paths, path scoping and command policy each have one
module (`plan.py`, `repo.py`, `verify.py`, `paths.py`, `tools/context.py`, `tools/policy.py`).
`workflow.py` only orchestrates; the CLI and the dashboard server call those modules and never
a private `Guild` method. Results that cross a module boundary are objects (`Verification`,
`ToolResult`, `TaskOutcome.reviews`), not dicts or text to be searched. New code follows the
same question: should these pieces change together? The reasoning for each move is in
`docs/UNTANGLE.md`.

## ADR-018 One guild process per project (accepted 2026-11-12)
Planning, running tasks and pairing switch branches, edit files and rewrite plan.json, and
two dashboards on one folder are easy to get (a terminal's `guild ui` plus the VS Code
extension's). Those entry points take `ProjectLock`: an OS file lock on `.guild/lock`
(`fcntl.flock` / `msvcrt.locking`), so a crashed process never leaves a stale lock. Read-only
work (chat, ask, review, status) takes no lock.

## ADR-019 No known price, no call under a cap (accepted 2026-11-12)
The router refused models with a list price under a $0 cap, but a hosted model missing from
the price table was costed at $0 and passed any cap. A cap that cannot be computed is not
kept, so such a model is refused before the request with a message pointing to
`.guild/prices.yaml`. Local endpoints are exempt; a profile without a cap is unaffected.
