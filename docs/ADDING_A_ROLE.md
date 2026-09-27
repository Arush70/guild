# Adding a role

A role is one YAML file. Create `.guild/roles/<name>.yaml` in your project (project-local) or
`src/guild/data/roles/<name>.yaml` (built-in, send a PR).

```yaml
name: ux                      # used in roles_enabled and `guild ask ux "..."`
title: UX Reviewer
slot: reasoner                # which profile slot fills this seat: frontier | coder | reasoner | cheap
escalation_slot: null         # optional: slot to use after repeated failures (engineer uses coder_escalation)
tools: [read_file, list_files, search, git_diff]   # least privilege; see tools/registry.py
max_tool_calls: 25
write_allowlist: null         # e.g. ["**/*.md"] to restrict writes; null = all (only if write tools are listed)
description: >
  One paragraph shown in `guild roles`.
system_prompt: |
  You are the UX Reviewer ...
  Reply with exactly this JSON and nothing else:
  {"verdict": "approve" | "request_changes", "findings": [...], "summary": "..."}
```

Rules of thumb:

- **Strict output contract.** End the prompt with the exact JSON shape. If it matches an
  existing contract (review, plan, engineer, docs, research), register it in
  `src/guild/schemas.py::SCHEMAS` so replies are validated and repaired automatically.
  Unknown roles accept any JSON object.
- **Least privilege.** Reviewers get read tools only. Only the engineer gets `write_file`,
  `edit_file`, `run_command`. Docs gets writes with a Markdown allowlist.
- **Enable it.** Add the name to `roles_enabled` in `.guild/config.yaml` if it should run in
  the task loop, or just use it with `guild ask <name> "..."`. Reviewer roles named in
  `roles_enabled` are not automatically wired into `run_task` yet — the built-in reviewers
  are `critic` and `security`; `performance` can be run with `guild ask performance "..."`
  or `guild watch --roles critic,security,performance`.
- **Test it** with `FakeProvider` in `tests/` — no API calls in tests.
