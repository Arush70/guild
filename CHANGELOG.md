# Changelog

## Unreleased
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
