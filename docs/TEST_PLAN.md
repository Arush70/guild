# Test Plan — guild

"Working" means all three layers pass.

## 1. Unit / integration (automated, no API)

`ruff check src tests && pytest -q` — 59 tests. Coverage by area:

- **Workflow** (`tests/test_workflow.py`): plan saves tasks; happy path accepts and documents;
  revision rounds + escalation; security block prevents acceptance; fallback chain; read-only
  roles can't write; docs allowlist; path escape refused; selected task order; verification
  detects real failures; text tool-calls executed; `.guild` never committed; untracked files
  tolerated; dirty tracked tree → no branch, no commit; merge/discard helpers; engineer
  "done" with no changes → blocked; false "blocked" → push-back → recovers; pytest exit 5 ok;
  unfinished dependency skipped; crash records traceback.
- **Robustness** (`tests/test_robustness.py`): schema coercions and errors; repair loop;
  give-up after 2 repairs; on_token streaming; OpenAI stream assembly.
- **Hardware / init** (`tests/test_hardware.py`): model recommendations for 8 GB / 24 GB /
  CPU-only; profile building with keys; missing pulls; test-command detection; wizard
  interactive and `-y`; git init offer.
- **UI server** (`tests/test_ui.py`): state/doctor; plan → run via API; job conflict 409;
  task edit/delete; hub replay; files + file read + path escape 403; project switch + fs;
  chat round-trip with memory; revise plan keeps done tasks; state reports root.
- **Units** (`tests/test_units.py`): profiles/roles load and reference known tools/slots;
  free profile has no paid models; extract_json; cost pricing; vague done_when; text
  tool-call variants.

## 2. Dashboard smoke (headless Chromium, scripted fake provider)

Script pattern in this session's history (`fakeplug3.py`): start `serve()` with a
`FakeProvider` patched into `Router._provider_for`, then with Playwright:

- page loads, no JS errors, header shows the project path
- Files tab lists files; clicking one opens the viewer
- Chat: send → assistant message appears (streamed)
- Goal → plan → tasks render; Plan check shows warnings for vague tasks
- run next task → live pane visible → task `done` → diff modal shows the change →
  trace modal lists per-role costs and a timeline → merge → branch gone, main has the commit
- project picker opens and lists directories
- dark and light colour schemes both render

## 3. Real-model acceptance (manual, the v1.0 gate)

Environment: owner's laptop, Ollama, free profile written by `guild init`.

| # | Check | Pass when |
|---|---|---|
| R1 | `guild doctor` in Demo | every Ollama row `ready`, `git: yes` |
| R2 | plan "Add multiply(a,b) to app.py + test_multiply" | 1 task, no Plan-check warnings |
| R3 | run next task | engineer writes files (tool_call events with `write_file`/`edit_file`), tests PASS, critic+security approve, lead ACCEPT, branch created |
| R4 | diff → merge | main has multiply + test; branch deleted |
| R5 | plan "build a calculator" → run all | 4/4 accepted, each merged; total < 30 min |
| R6 | repeat R5 from a fresh folder ×3 | 3/3 |
| R7 | lite profile with ANTHROPIC_API_KEY | R3 passes; `guild cost` ≤ cap; Lead is Claude in trace |
| R8 | chat: "what does app.py do?" | trace shows `list_files`/`read_file` before the answer; answer cites the file |
| R9 | `guild watch --once` on Demo | critic + security print verdicts |
| R10 | wheel install in clean venv | `guild init -y` + `guild doctor` work |

Record results in `docs/MEMORY.md`.

## 4. Safety checks (run with every release)

- Try a task whose description says "delete ../../something" → tool refused in trace.
- Set `GROQ_API_KEY` with free profile → router never calls a priced model.
- Dirty tracked file in project → "will NOT commit" warning; `git log` unchanged.
- `.guild/` never in `git ls-files` after a run.
