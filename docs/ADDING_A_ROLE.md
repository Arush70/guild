# Adding a role

A role is one YAML file. Create `.codeguild/roles/<name>.yaml` in your project (project-local) or
`src/codeguild/data/roles/<name>.yaml` (built-in, send a PR).

```yaml
name: ux                      # used in roles_enabled and `codeguild ask ux "..."`
title: UX Reviewer
slot: reasoner                # which profile slot fills this seat: frontier | coder | reasoner | cheap
escalation_slot: null         # optional: slot to use after repeated failures (engineer uses coder_escalation)
tools: [read_file, list_files, search, git_diff]   # least privilege; see tools/registry.py
max_tool_calls: 25
review: Review the change for the people who will use it.   # makes it a reviewer (see below)
write_allowlist: null         # e.g. ["**/*.md"] to restrict writes; null = all (only if write tools are listed)
description: >
  One paragraph shown in `codeguild roles`.
system_prompt: |
  You are the UX Reviewer ...
  Reply with exactly this JSON and nothing else:
  {"verdict": "approve" | "request_changes", "findings": [...], "summary": "..."}
```

Rules of thumb:

- **Strict output contract.** End the prompt with the exact JSON shape. If it matches an
  existing contract (review, plan, engineer, docs, research), register it in
  `src/codeguild/schemas.py::SCHEMAS` so replies are validated and repaired automatically.
  Unknown roles accept any JSON object.
- **Least privilege.** Reviewers get read tools only. Only the engineer gets `write_file`,
  `edit_file`, `run_command`. Docs gets writes with a Markdown allowlist.
- **Make it a reviewer.** A role with a `review:` line is a reviewer: when its name is in
  `roles_enabled` (`.codeguild/config.yaml`, or the dashboard's settings), it reviews every
  task's change after the tests, and its verdict counts like the critic's — a
  `request_changes` sends the engineer another round. Its reply is checked against the review
  format (`verdict`, `findings`, `summary`) and repaired if needed. `codeguild review` runs all
  enabled reviewers on the current diff. Without `review:`, use the role with
  `codeguild ask <name> "..."`.
- **Test it** with `FakeProvider` in `tests/` — no API calls in tests.
