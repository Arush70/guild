# Changelog

## 0.2.0 — 2026-09-27
- Output contracts: every role's reply is validated (pydantic) and repaired with the error fed back (≤2 retries).
- Streaming: model output streams live into the dashboard (OpenAI-compatible and Anthropic).
- Dashboard: per-run trace viewer (timeline + per-role cost), per-task diff view with merge/discard.
- Branch safety: guild never commits unless on a branch it created; untracked files tolerated and kept out of its commits.
- `guild init` wizard: detects GPU/RAM/Ollama/keys/test runner and writes a profile that fits the machine.
- `guild doctor` shows machine summary, recommended models and missing pulls.
- New roles: `kaggle` (competition strategist) and `performance` (reviewer).
- `guild watch`: review every new commit; `docs/github-action.yml` for PR reviews in CI.
- docs: TROUBLESHOOTING.md, ADDING_A_ROLE.md.

## Unreleased
- Engineer that reports done without changing any file is asked again, then blocked (never falsely accepted).
- Engineer prompt: code in the reply is discarded; tools are the only way to change files.
- Crashes inside a task write an `error` event with traceback to the trace and the dashboard.
- Streaming parser hardened for Ollama quirks (missing tool-call index, reasoning-only chunks).
- Verification is deterministic: guild runs the test command itself (no LLM verifier in the loop).
- Text tool-call fallback: `{"name": ..., "arguments": ...}` written as plain text by small models is executed.
- `.guild/` is never committed or included in review diffs.
- `edit_file` rejects empty `old_text` with a helpful message.
- Selected tasks all run; only dependents of a failed task are skipped.
- Lead prompt: tasks must be concrete code changes with checkable `done_when`; questions only when blocking.
- Engineer prompt: implement a minimal version instead of blocking on vague tasks.
- Plan check: warns about tasks whose `done_when` is not verifiable (CLI + dashboard).
- Run several tasks at once: `guild run T1 T3`, dashboard checkboxes + **run selected**.
- `guild ui`: local web dashboard with live agent activity, plan editing, doctor and cost panels (`pip install guild-ai[ui]`).
- Atomic plan.json writes; `.gitignore` edits no longer count as a dirty tree.

## 0.1.0 — 2026-09-20
- First release: lead / engineer / verifier / critic / security / docs / researcher roles,
  free / lite / pro profiles, fallback router with cost tracking, JSONL traces,
  local and Docker sandboxes, CLI (`init doctor plan status run review ask cost roles`).
