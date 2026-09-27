# Architecture — guild

## Stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.10+ | owner's stack; matches ML/Kaggle work |
| CLI | typer + rich | small, testable with CliRunner |
| Config/contracts | pydantic v2 + YAML | validation with lenient coercions for weak models |
| Providers | `openai` SDK (any OpenAI-compatible API) + `anthropic` SDK | covers Ollama, Groq, Gemini, DeepSeek, OpenRouter, OmniRoute, LiteLLM, Anthropic |
| Dashboard | FastAPI + uvicorn, single `index.html`, SSE | no build step, no node, runs in the CLI process |
| Tests | pytest with scripted `FakeProvider` | no API calls in tests, ever |
| Packaging | setuptools, `guild-ai` on PyPI (planned) | data files (roles/profiles/html) ship in the wheel |

## Data flow

```
owner goal
   │
   ▼
Lead (frontier slot) ──► plan.json  (roadmap, tasks with done_when, deps)
   │                                       ▲
   ▼                                       │ revise_plan(instruction)
run_task(T)                                │
   ├─ _start_branch  guild/<id>-<slug>  (only if tracked tree is clean; else no commits at all)
   ├─ Engineer (coder slot; coder_escalation after N failed rounds)
   │     tools: read/write/edit/search/run_command/run_tests/git  — scoped to project root
   │     text tool-calls executed; "done" with no file change → pushed back → blocked
   │     first "blocked" → pushed back once
   ├─ verify()   guild runs cfg.test_command itself (pytest exit 5 = no tests, ok)
   ├─ Critic + Security (reasoner slot, read-only)  → verdict + findings (validated)
   ├─ revise loop (≤ limits.max_revision_rounds)
   ├─ Lead review → ACCEPT / REVISE / REPLAN
   └─ Docs (cheap slot, *.md only)
   │
   ▼
branch left for the human: dashboard diff → merge (git merge --no-ff) or discard
```

Every model call, tool call, verification, decision, fallback, note and error is appended to
`.guild/runs/<run-id>/trace.jsonl`. The dashboard hub mirrors trace events over SSE and adds
throttled `token` events for streaming.

## Modules

```
src/guild/
  cli.py          commands; init wizard (hardware → profile), doctor, plan, run, review, ask,
                  cost, roles, watch, ui
  workflow.py     Guild: plan, revise_plan, run_task/_run_task, run_tasks, verify, chat, ask,
                  branch helpers (default_branch, branch_diff, merge_branch, discard_branch)
  agent.py        Agent.run: system + context + prior + prompt → tool loop → parse → validate
                  → repair (≤2). extract_json, extract_text_tool_calls.
  schemas.py      output contracts per role/job (PlanOutput, EngineerOutput, ReviewOutput …)
  config.py       Profile / Role / ProjectConfig; .guild/ overrides beat package data
  hardware.py     detect(); recommend_local(); build_profile(); detect_test_command()
  providers/      base types; openai_compat (+streaming); anthropic (+streaming);
                  router (fallback chain, sticky last-good, cost cap, CostTracker)
  tools/registry  @tool functions with JSON schemas; ToolContext enforces root scoping,
                  read-only roles, write allowlists
  sandbox.py      LocalSandbox / DockerSandbox; secret env vars stripped
  trace.py        JSONL writer/reader
  ui/server.py    FastAPI app, _State (switchable root), Hub (jobs + SSE + token batching)
  ui/index.html   the dashboard (vanilla JS, CSS variables, dark/light)
  data/roles/     *.yaml   data/profiles/  free|lite|pro.yaml
```

## Architectural rules

- **Roles are data.** Behaviour changes go in YAML prompts first; code only for mechanics.
- **Tools are the only side effects.** Models never touch disk/shell except through
  `tools/registry.py`, and every tool resolves paths inside the project root.
- **Verification is not an LLM.** Tests are run by guild; a model may narrate, never decide.
- **The human merges.** guild creates branches and commits on them; it never merges, pushes,
  or commits on a branch it did not create.
- **Every model reply has a contract** (`schemas.py`). Unknown roles are free-form.
- **Small-model tolerance is a feature**, not a hack: text tool-calls, lenient coercions,
  repair prompts, push-backs. Keep them.
- **No network in tests.** `FakeProvider` or a tiny custom provider class.
- **State on disk is atomic** (`os.replace`) because the UI reads while jobs write.
- **One process.** The dashboard runs in the CLI process; no daemon, no database.

## Extension points

- New provider: one `Endpoint` line in `providers/openai_compat.py::ENDPOINTS`.
- New role: `data/roles/<name>.yaml` (+ optional schema in `schemas.py::SCHEMAS`).
- New tool: `@tool(...)` in `tools/registry.py`; list it in a role's `tools:`.
- New dashboard feature: endpoint in `server.py` + section in `index.html`; events via Hub.
