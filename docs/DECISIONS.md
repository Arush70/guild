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
