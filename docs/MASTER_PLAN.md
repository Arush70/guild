# guild — Master Plan

**The single document that explains what guild is, why it exists, how it is built, everything
done so far, where it stands today, and everything still to do — with the strategies behind
each decision.** Read this first; the other docs (`docs/PRD.md`, `docs/ARCHITECTURE.md`,
`RULES.md`, `TASKS.md`, `docs/MEMORY.md`, `docs/DECISIONS.md`) are the detailed references it
points to.

- Repo: https://github.com/Arush70/guild (MIT)
- Owner: Arush Kumar Vishwakarma (MSc, University of Exeter)
- Version: 0.2.x (unreleased changes on `main`), last updated 2026-10-07
- Status in one line: **feature-complete alpha; proven once on real local models; the next
  milestone is repeatable end-to-end acceptance on the free profile, then PyPI 1.0.**

---

## 1. Vision

### 1.1 The problem

Agentic coding tools (Claude Code, Cursor, Copilot Workspace) are excellent but expensive,
closed, cloud-only, and give you a single agent. Students, hobbyists and small teams want:

- something that runs **free on their own GPU** (Ollama) and gets *better*, not *different*,
  when they add a £10 API key;
- a **team** — someone to plan, someone to code, someone to criticise, someone to check
  security, someone to keep the docs honest — instead of one model talking to itself;
- **safety by construction**: never merge to `main` by itself, never spend beyond a cap,
  never touch files outside the project, never leak API keys into a subprocess;
- **auditability**: a trace of every model call, tool call, token and dollar.

### 1.2 The product

`guild` is a budget-aware multi-agent coding team. You install it with `pip install guild-ai`,
run `guild init` in any project, and either:

- use the CLI: `guild plan "goal"` → `guild run` → review branch → merge, or
- use the dashboard: `guild ui` → type a goal → tick tasks → run → watch live → diff → merge.

Three **profiles** map the same team onto different budgets:

| profile | monthly cost | who does what |
|---|---|---|
| `free` | £0 | everything on local Ollama models (7–8b class); optional free cloud keys (Groq, Gemini) for the Lead |
| `lite` | ~£10–20 | Claude plans, reviews and escalates; the local model does most coding |
| `pro`  | pay-as-you-go | the best available model in every seat |

### 1.3 Who it is for (in priority order)

1. **The owner** — daily driver for his own projects and Kaggle work; the dissertation (MACS)
   shares the JSONL trace discipline.
2. Students / indie developers with a gaming laptop and no budget.
3. Small teams who want a reviewable, reversible AI workflow with a human merge gate.

### 1.4 What "done" means (v1.0 success criteria, from `docs/PRD.md`)

1. The 4-task calculator demo is accepted end to end **3 times out of 3** on the `free`
   profile with local models, from a fresh folder, using *run all*.
2. The same demo passes on `lite`, with cost under `max_usd_per_run` and `guild cost` within
   20 % of the provider dashboard.
3. Two further small real projects get at least one task accepted.
4. `pip install guild-ai` in a clean venv → `guild init -y` → `guild doctor` works on Windows,
   macOS and Linux.
5. The safety model holds under adversarial prompts (see §4.6).

---

## 2. Principles and strategies (why things are the way they are)

These are the standing strategies. Each is backed by an ADR in `docs/DECISIONS.md`.

| # | Strategy | Why |
|---|---|---|
| S1 | **Behaviour lives in YAML, code only does mechanics** (ADR-002) | Anyone can add or tune a role/profile without forking; prompt changes are reviewable diffs. |
| S2 | **Abstract slots + fallback chains** (ADR-003): `frontier / coder / coder_escalation / reasoner / cheap` → list of `provider/model` | Decouples "quality needed" from "vendor"; unreachable model → next candidate; `free` never lists a priced model and the router refuses one when the cap is $0. |
| S3 | **Deterministic verification** (ADR-004) | The first LLM "verifier" lied. guild runs the test command itself; a model may explain a failure, never declare success. |
| S4 | **Tolerate small models, don't trust them** (ADR-005, 006, 011) | 7b models write tool calls as text, return near-miss JSON, give up on missing files and claim "done" without changes. guild executes text tool calls, coerces/repairs JSON (≤2 repairs), pushes back once on "blocked"/"done-without-change", and only then believes them. |
| S5 | **The human merges** (ADR-007) | AI code must be reviewable and reversible. guild branches `guild/<task>`, commits there, never merges or pushes. |
| S6 | **Hard budget cap before every call** (ADR-008) | No model in the loop can spend past `max_usd_per_run`. |
| S7 | **One process, one file UI, no build step** (ADR-009) | `pip install` is the whole install; SSE not websockets. |
| S8 | **Flat JSONL traces** (ADR-010) | pandas-loadable; tokens, cost, provenance on every call; same discipline as the dissertation. |
| S9 | **Read-only reviewers, write-scoped roles** | Critic/security/verifier/assistant/performance cannot change files and can run only inspection commands; docs may write only `*.md`. |
| S10 | **Tests never call real APIs** | `FakeProvider` scripts model replies per role; the whole loop is testable in seconds and CI needs no keys. |
| S11 | **Real-model runs drive prompt work** | Every prompt change is motivated by a trace from a real run and reproduced in a FakeProvider test before it is called fixed. |
| S12 | **Docs as context** | A project's PRD/ARCHITECTURE/RULES/TASKS/MEMORY/DECISIONS are fed into every role; the Docs role keeps MEMORY/TASKS current. The guild repo eats its own dog food (this file, CLAUDE.md, etc. are written for Claude Code sessions). |
| S13 | **Add features as roles/profiles first, code second** | Keeps the core small; the `kaggle`, `performance`, `assistant` roles are pure YAML. |

---

## 3. Architecture (how it works)

```
src/guild/
  cli.py              typer: init(wizard) doctor plan status run review ask cost roles watch ui
  workflow.py         Guild: plan() revise_plan() run_task() run_tasks() verify() ask() chat()
  agent.py            one role's tool-calling loop; extract_json; extract_text_tool_calls;
                      validates the final reply (schemas.py) and asks for repairs (≤2)
  schemas.py          pydantic contract per role/job, lenient coercions for small models
  config.py           Profile / Role / ProjectConfig + loaders (.guild/ overrides win)
  hardware.py         detect GPU/RAM/Ollama/keys; recommend_local(); build_profile(); test cmd
  trace.py            JSONL writer/reader (.guild/runs/<id>/trace.jsonl)
  sandbox.py          LocalSandbox / DockerSandbox; secrets stripped; process-tree kill
  tools/registry.py   read_file write_file edit_file list_files search run_command run_tests
                      git_status git_diff web_fetch — scoped to the project root
  providers/          openai_compat (Ollama, Groq, Gemini, DeepSeek, OpenRouter, Mistral,
                      OmniRoute, LiteLLM, custom), anthropic_provider, router (fallback +
                      cost tracking + streaming passthrough)
  ui/server.py        FastAPI + SSE Hub; ui/index.html single-file dashboard
  data/roles/*.yaml   lead engineer critic security verifier docs researcher performance
                      kaggle assistant
  data/profiles/*.yaml free lite pro
  data/templates/*.md PRD/ARCHITECTURE/RULES/TASKS/MEMORY/DECISIONS for `guild init --docs`
tests/                74 tests, FakeProvider, headless-browser dashboard checks
```

### 3.1 The loop per task

```
Lead PLAN (JSON tasks with files, done_when, depends_on)
  └─ for each runnable task:
       git checkout -B guild/<id>-<slug>          (only if tracked tree is clean, HEAD exists)
       collect baseline pytest node ids           (so deleted tests are detected)
       Engineer implements with tools             (text tool calls executed; read-before-write)
         ├─ "done" with no file change → push back once → then blocked
         └─ "blocked" → push back once with facts → then believed
       guild runs the test command itself         (exit 5 = no tests is OK; removed tests FAIL)
       Critic + Security review the diff          (read-only; no-JSON reply = request_changes)
       loop ≤ max_revision_rounds; escalate to coder_escalation after escalate_after failures
       Lead ACCEPT / REVISE / REPLAN
       Docs updates Markdown (only *.md)
       commit on the guild branch; accepted → stay on branch (dependents stack);
       rejected/crashed → return to the start branch
Human: dashboard diff → merge (or git merge)
```

### 3.2 Safety model (what must never break)

- Tools resolve inside the project root; `..` is refused.
- Read-only roles: no write tools; `run_command` limited to an allowlist of inspection
  commands (pytest, git log/show/diff/status, grep/rg, ls/cat/head/tail, ruff check, mypy,
  pip list/show/audit, bandit…). Pipes allowed; `;`, `&&`, `>`, backticks, `--fix` refused.
- Docs may write only `*.md`.
- `*KEY*`, `*TOKEN*`, `*SECRET*`, `*PASSWORD*`, `*CREDENTIAL*` are stripped from every
  subprocess environment.
- `.guild/` is never committed by guild and never appears in review diffs; caches
  (`__pycache__`, `.pytest_cache`, `*.pyc`, `.ruff_cache`, `.mypy_cache`) are never committed.
- `_commit` is a no-op unless guild created the branch; pre-existing untracked files are
  excluded from guild's commits; no branching on an unborn HEAD.
- `free` profile refuses any model with price > 0; the cap is checked before every call.
- `write_file` on an existing unread file is refused (read first); deleting an existing
  test fails verification automatically.

### 3.3 The dashboard (`guild ui`)

Project picker (browse folders, recent projects, switch live) · Doctor (model reachability,
machine summary) · Plan with per-task checkboxes, *run next* / *run selected* / *run all*,
*revise plan* by prompting · live activity log with streamed tokens per role · team panel
(state per role, cost) · Files browser/viewer · Chat with the read-only Assistant (reads files
before answering) · Ask a role · Runs list with per-run trace viewer and per-role cost ·
per-task diff modal with **merge** / **discard** · git-missing warning banner · dark/light.

### 3.4 Roles (YAML)

| role | slot | tools | may write | job |
|---|---|---|---|---|
| lead | frontier | read/list/search/run_tests | – | plan, revise plan, final review, questions only when worth asking |
| engineer | coder (→ coder_escalation) | all file tools, run_command, run_tests, git | any | implement one task; read before write; create missing files |
| critic | reasoner | read/list/search/git_diff | – | correctness/style review of the diff |
| security | reasoner | + run_command (allowlist) | – | secrets, injection, unsafe deps, privacy |
| verifier | cheap | run_tests/run_command | – | legacy (verification is deterministic now) |
| docs | cheap | file tools | `*.md` only | README/CHANGELOG/MEMORY/TASKS after acceptance |
| researcher | frontier | read/search/web_fetch | – | options + sources |
| performance | reasoner | read/git_diff/run_command | – | hot paths, complexity |
| kaggle | frontier | read/search/web_fetch | – | competition strategy |
| assistant | frontier | read/list/search/run_tests/run_command | – | chat about the code (prose, not JSON) |

---

## 4. What has been done (chronological)

### Phase 0 — Design and skeleton (0.1.0)
- Chose Python, MIT, the name `guild`, PyPI name `guild-ai` (plain `guild` is Guild AI).
- Core loop, YAML roles and profiles, router with fallback chains + cost cap, JSONL traces,
  local/Docker sandboxes, tool registry with root scoping, CLI (`init doctor plan run review
  ask cost roles`).
- First real run on Ollama `qwen2.5-coder:7b` exposed: tool calls written as text, an LLM
  verifier that faked passes, `.guild/plan.json` leaking into commits. → S3, S4 born.

### Phase 0.5 — Product surface (0.2.0)
- Dashboard with SSE streaming, trace viewer, diff/merge/discard, plan checkboxes,
  multi-select run.
- Output contracts (pydantic) with lenient coercion and repair loop.
- Token streaming through both provider types, coalesced in the Hub.
- `guild init` wizard: hardware detection (nvidia-smi, RAM, Ollama, keys), machine-specific
  profile override in `.guild/profiles/`, test-command detection, `git init` offer.
- `guild doctor` with machine summary and missing pulls; `guild watch` (review every commit);
  GitHub Action for PR review; `kaggle` + `performance` roles; TROUBLESHOOTING and
  ADDING_A_ROLE docs.
- Branch safety: commit only on guild-created branches; untracked files tolerated and
  excluded; atomic plan writes; `/api/state` ordering fix (it was a race).

### Phase 0.7 — Owner feedback round (0.2.x)
Driven by real runs in the Demo folder and the owner's notes:
- Engineer blocked on "app.py does not exist" → task brief lists missing files; first
  "blocked" pushed back once with the facts.
- "done" without any file change → asked again, then blocked (never falsely accepted).
- Run-selected ran T3 before T2 → dependency gating in `run_tasks`.
- pytest exit 5 (no tests) is not a failure. Empty, git-less Demo → init offers `git init`;
  dashboard warns.
- "Where is the code saved / can't read my project / no chatbot / can't edit the plan" →
  project picker, Files panel, Chat with Assistant role, *revise plan* by prompt.
- Lead told to ask only questions that are worth asking.
- Vibe-coding doc set adopted for the repo itself (PRD, ARCHITECTURE, DESIGN, RULES, TASKS,
  DECISIONS, MEMORY, TEST_PLAN, SECURITY) and `guild init --docs` scaffolds the same for
  user projects; roles read them as context (S12).
- Owner moved day-to-day development to Claude Code; `CLAUDE.md` + read-order + first-prompt
  written for that.

### Milestone — first real ACCEPT (2026-09-27, TASK-001)
On `free` with `qwen2.5-coder:7b`: Engineer emitted three text tool calls, guild executed two
`write_file`s and `run_tests` (2 passed), critic/security/lead approved, branch
`guild/t1-add-multiply-function-and-test` created, cost $0.00. **But** it rewrote `app.py`
and `test_app.py` without reading them and deleted the existing `add`/`test_add`.

### TASK-002 — guards for what TASK-001 exposed (landed 2026-10-02)
- `write_file` on an existing, unread, non-empty file is refused with instructions; files
  written earlier in the same task stay writable (tracked via `files_read`/`files_written`).
- Baseline pytest node ids are collected before the engineer starts; if any disappears,
  verification fails with `EXISTING TESTS REMOVED: <names>` (non-pytest: count must not drop).
- Engineer prompt: read before write; keep every existing function/class/test.
- Two FakeProvider regression tests reproduce the 7b behaviour.

### Full code audit (2026-10-02) — 7 commits, 61 → 74 tests
Triggered by "check my complete code base and resolve all the problems". Findings and fixes:

**Tooling / consistency**
- The "92 ruff findings" on the owner's machine vs 0 elsewhere were *ruff default-rule drift
  between versions*, not code problems. Rule set is now pinned in `pyproject.toml`
  (`E F W I UP B SIM C4 RUF`, line length 100, `*.md`-style per-file ignores); the whole
  codebase is `ruff format`ted; CI runs `ruff check` **and** `ruff format --check`.
- SPDX licence metadata (setuptools ≥ 77); wheel built and installed in a clean venv,
  `guild init -y` and `guild doctor` verified (TASK-011 on Linux).

**Windows**
- Subprocess output decoded as UTF-8 with replacement (cp1252 crashed on `—`/`✓` in pytest
  output); child env forces `PYTHONIOENCODING`/`PYTHONUTF8`.
- A hung test run now kills the whole process tree (`taskkill /F /T`; POSIX `killpg`) —
  before, `subprocess.run` only killed `cmd.exe` and guild hung forever.

**Correctness (the 14 defects)**
1. Dashboard *merge* wrote `task.merged` into `plan.json`; `Task(**t)` could not load it →
   every later run/chat/status died. `Task.merged` added; unknown keys ignored.
2. `__pycache__`/`.pytest_cache` created by verification were committed to guild branches;
   after one merge they became tracked-and-modified → "dirty" → guild silently stopped
   branching forever. Excluded via git pathspecs; `guild init` gitignores them.
3. Switching project replaced the SSE Hub, orphaning open streams → dashboard stuck on
   "running". Hub is reset in place now.
4. A critic/security reply that never parsed (3 tries) was `None` and counted as **approve**.
   Now `request_changes` with a trace note.
5. Read-only roles enforced only a deny-regex that `echo x > f`, `sed -i`, `python -c`
   bypassed; `assistant`/`performance` weren't read-only at all. Now `Role.readonly: true` and
   a command allowlist.
6. Fresh `git init` (no commits) → garbage base ref, no `main`, and a "successful" self-merge.
   `guild init` makes the initial commit; `_start_branch` refuses an unborn HEAD;
   `merge_branch` refuses base == branch and surfaces git errors.
7. Tasks stacked on each other even after a rejected task. Rejected/crashed tasks now return
   to the start branch; accepted ones stay so *run all* can stack dependents.
8. `base_ref` was captured before the branch switch; now after, and only when a real hash.
9. Dangling `depends_on` ids from small models (`"none"`, `"T0"`, `"T1 "`) raised `KeyError`;
   normalised in the plan schema.
10. `null` / number / object values in string fields (`"summary": null`, `"tests_run": 2`)
    failed validation and burned repair rounds; coerced now.
11. `<think>…</think>` blocks with braces defeated JSON extraction; stripped, and the last
    balanced object wins.
12. Docs role was told to update `TASKS.md` but its allowlist (`**/*.md`) refused root-level
    files; `*.md` added.
13. 180 s client timeout killed long local generations in the non-streaming CLI path; local
    endpoints get 15 min (`GUILD_MODEL_TIMEOUT`).
14. Rich markup in model text (`[/tmp]`) crashed the CLI; escaped. Job start is check-and-set
    under the lock; per-task *run* buttons disabled while busy. `_norm()` stripped the dot off
    `.github/...` paths; fixed.

---

## 5. Where we are today (2026-10-07)

| area | state |
|---|---|
| Code | `main` at `149030b`; 74 tests green; ruff + format clean; wheel builds |
| Real-model proof | 1 accepted run on `free` (before the guards); **guards + audit not yet confirmed on a real run** |
| v1.0 gate | criterion 1: 1/3 (and that one must be re-done with guards); 2–3: not started; 4: Linux only; 5: holds in tests |
| PyPI | not published (name `guild-ai` reserved in pyproject; availability still to check) |
| Owner workflow | Claude Code (VS Code, Windows) using `CLAUDE.md` → `TASKS.md` one task per session |
| Known unknowns | push-backs, streaming parser on Ollama, validation/repair, branch return — all unconfirmed on real models; chat quality on `free` is shallow (7b in the `frontier` slot); chat history is in memory only |

**Immediate next actions for the owner**

```powershell
cd "C:\Machine Learning projects\guild"
& .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
ruff check src tests; ruff format --check src tests; pytest -q    # expect 74 passed
git push

cd "C:\Machine Learning projects\Demo"
git checkout master
git branch -D guild/t1-add-multiply-function-and-test   # it deleted add()
ollama pull qwen2.5-coder:3b
guild ui
```
Then re-run T1, read `.guild/runs/<latest>/trace.jsonl`, confirm `read_file("app.py")`
precedes `write_file` and `test_add` survives. Record the result in `docs/MEMORY.md`.

---

## 6. What we are going to do (the roadmap in detail)

Numbering matches `TASKS.md`. One task per Claude Code session: implement → `pytest` → review
diff → commit → tick → update `docs/MEMORY.md` and `CHANGELOG.md`.

### Phase 1 — Prove the loop on real local models (v1.0 gate)

| task | what | done when | strategy |
|---|---|---|---|
| TASK-001 ✅ | Demo T1 on `free`, read trace | accepted once | — |
| TASK-002 ✅ | Guards for what T1 exposed | tests reproduce the 7b behaviour | S11 |
| TASK-003 | 4-task calculator plan, **run all** | every task accepted and merged; wall time + tokens noted | start by re-running T1 with the guards; discard the old branch first; expect prompt tweaks, not architecture |
| TASK-004 | Repeat TASK-003 ×3 from a fresh folder | 3/3 | if a run fails, the trace is the bug report: reproduce with FakeProvider, fix, re-run |
| TASK-005 | Same demo on `lite` with `ANTHROPIC_API_KEY` | cost < `max_usd_per_run`; `guild cost` within 20 % of the dashboard | Claude in `frontier`/`reasoner`/`coder_escalation`, local in `coder` |
| TASK-006 | Chat quality | Assistant reads files before answering on 5 questions | check trace for `list_files`/`read_file`; tune the prompt; recommend a free Groq/Gemini key for `frontier` on `free` |

Expected trouble spots (and the plan for each):
- 7b Lead writes vague `done_when` → `Plan.warnings()` flags it; dashboard shows it; consider
  a stricter Lead prompt with examples.
- deepseek-r1 critic emits long `<think>` → now stripped; if it still fails to produce JSON,
  swap the `reasoner` slot to `qwen3:8b` in the generated profile.
- Engineer edits with `edit_file` and misquotes `old_text` → error message tells it to re-read
  and copy exactly; if frequent, add a fuzzy-match fallback with a single candidate.

### Phase 2 — Release

| task | what |
|---|---|
| TASK-010 | Check PyPI name `guild-ai`; fallbacks `guild-agents`, `codeguild` (console script stays `guild`) |
| TASK-011 | `python -m build` → clean venv → `guild init -y` → `guild doctor` on **Windows** (Linux done 2026-10-02) |
| TASK-012 | Publish `0.3.0` to TestPyPI then PyPI; tag `v1.0.0` once Phase 1 is 3/3 |
| TASK-013 | README screenshot from a real accepted run + 30-second GIF of plan → run → merge |

Release checklist: CHANGELOG moves "Unreleased" under the version; `pyproject` version bump;
`git tag`; GitHub release notes; announce (Reddit r/LocalLLaMA, Ollama Discord, Exeter CS).

### Phase 3 — Product depth

| task | what | notes |
|---|---|---|
| TASK-020 | Verify `guild init --docs` scaffolding on Windows | templates exist; path handling check |
| TASK-021 | Wire extra reviewers from `roles_enabled` (e.g. `performance`) into `run_task` | generic reviewer loop instead of hard-coded critic/security |
| TASK-022 | Per-task git worktrees | prerequisite for parallel engineers |
| TASK-023 | `ux` and `data-quality` reviewer roles | YAML only (S13) |
| TASK-024 | Optional nightly CI job against a real Ollama | non-blocking; guards against prompt regressions |
| TASK-025 | Persist chat to `.guild/chat.jsonl` | survives restarts |

### Phase 4 — Claude-Code-like capabilities (after Phase 1 is green)

| task | what | design |
|---|---|---|
| TASK-030 | `pair` role + approval gate | `ToolContext.approver`; mutating tools show a diff preview; server approval queue + SSE `approval_request` + `POST /api/approve/{id}`; modes ask-all / auto-edits / plan(read-only); `guild pair` in the CLI |
| TASK-031 | Chat context | `@path` attaches files; `# note` appends to MEMORY; history compaction with the cheap slot > 6k tokens |
| TASK-032 | Codebase index | `guild index` → `.guild/index.json` via `ast` (Python) + regex (js/ts/go/rust/java); ≤ 2k-token rendering in `project_summary()`; `find_symbol` tool |
| TASK-033 | MCP client | `.guild/mcp.json` (Claude Code shape); stdio + http; tools registered as `mcp:<server>:<tool>`, allowed per role by glob |
| TASK-034 | Commands + hooks | `.guild/commands/<name>.md` → `/name`; `after_write: ["ruff --fix {path}"]`, `deny_write: ["migrations/**"]` |
| TASK-035 | `guild commit` / `guild pr` | message from diff via cheap slot; `gh` CLI; trace link in the PR body |
| TASK-036 | `guild chat` terminal REPL | streaming with rich.live; same features as dashboard chat |
| TASK-037 | Parallel engineers | per-task worktrees, Lead marks independent tasks, N concurrent, merge queue (depends on TASK-030) |

### Later / ideas (not scheduled)
- VS Code extension talking to the dashboard HTTP API.
- `kaggle` end-to-end: download data, EDA notebook, baseline submission.
- Cost dashboard across runs; monthly budget tracking against `monthly_budget_gbp`.
- Multi-language test-runner detection beyond pytest/npm/cargo/go.
- Docker sandbox as the default for untrusted repos.

---

## 7. Working agreements

### 7.1 Development loop (for the owner and Claude Code)
1. Read `CLAUDE.md` → `docs/PRD.md` → `docs/ARCHITECTURE.md` → `RULES.md` → `TASKS.md` →
   `docs/MEMORY.md` → `docs/DECISIONS.md`. State understanding and plan before coding.
2. One task per session. Smallest change that meets *done when*.
3. `ruff check src tests && ruff format --check src tests && pytest -q` must pass.
4. Never run `guild` inside the guild repo itself (use `..\Demo`); `-C` goes **after** the
   subcommand; `.guild/` is gitignored in the tool repo.
5. Commit with a message that says *why*; tick `TASKS.md`; update `docs/MEMORY.md` and
   `CHANGELOG.md`; add an ADR before changing a permanent decision.
6. Real-model trace first, FakeProvider test second, prompt/code change third (S11).

### 7.2 Testing strategy (`docs/TEST_PLAN.md`)
- Unit: JSON extraction, text tool-call extraction, schemas, path scoping, allowlists.
- Workflow: FakeProvider end-to-end for happy path, escalation, push-backs, dependency gating,
  read-before-write, removed tests, reviewer failure, branch return, cache exclusion.
- UI: TestClient for every endpoint; headless Chromium smoke (streaming, plan, diff, merge).
- Real: Demo runs recorded in `docs/MEMORY.md`; optional nightly Ollama job (TASK-024).

### 7.3 Budget strategy
- `free`: $0 cap; Ollama only; recommend `qwen2.5-coder:7b` (coder), `deepseek-r1:8b` or
  `qwen3:8b` (reasoner), `qwen2.5-coder:3b` (cheap) for an 8 GB GPU; 27b+ models don't fit on
  the owner's laptop — never recommend them. Free Groq/Gemini keys lift the Lead for £0.
- `lite`: Claude in the seats where judgement matters (plan, review, escalation); local coder;
  per-run cap default $0.50; owner budget ~£10–20/month.
- `pro`: best model everywhere; per-run cap still enforced.

### 7.4 Owner's environment
Windows 11, i9-14900HX, 16 GB RAM, RTX 4070 mobile (8 GB VRAM). Ollama with
`qwen2.5-coder:7b`, `qwen3:8b`, `deepseek-r1:8b` (`qwen2.5-coder:3b` still to pull).
Repo `C:\Machine Learning projects\guild`, venv `.venv`
(`& .\.venv\Scripts\Activate.ps1`), `pip install -e ".[dev]"`. Demo project
`C:\Machine Learning projects\Demo` (branch `master`). Day-to-day via Claude Code in VS Code.

---

## 8. Risks and mitigations

| risk | impact | mitigation |
|---|---|---|
| 7b models cannot complete the calculator plan reliably | v1.0 gate slips | prompt tuning from traces; escalation slot; free cloud key for the Lead; accept "lite is the recommended default" if `free` plateaus at < 3/3 |
| Windows-only bugs unseen in Linux CI | owner hits crashes CI doesn't | UTF-8/timeout fixes landed; TASK-011 on Windows; consider a Windows runner in CI |
| Prompt regressions | silent quality loss | FakeProvider tests pin behaviour; nightly real-Ollama job (TASK-024) |
| PyPI name taken | rename churn | TASK-010 early; console script stays `guild` |
| Safety-model erosion as features grow (MCP, hooks, pair) | real damage to user repos | approval gate (TASK-030) before MCP/hooks; allowlists not deny-lists; tests for every refusal path |
| Scope creep (Phase 4) before Phase 1 | never ships | Phase 4 is explicitly gated on Phase 1 = 3/3 |

---

## 9. Glossary

- **profile** — YAML mapping abstract slots to a fallback chain of `provider/model` ids plus
  limits (`max_revision_rounds`, `escalate_after`, `max_tool_calls_per_task`, `max_usd_per_run`).
- **slot** — `frontier` (planning/review), `coder`, `coder_escalation`, `reasoner`
  (critique/security), `cheap` (docs, chat compaction).
- **role** — YAML with prompt, slot, tools, `write_allowlist`, `readonly`, `max_tool_calls`.
- **trace** — `.guild/runs/<id>/trace.jsonl`; kinds: `run_start phase model_call tool_call
  verification decision fallback note error task_end run_end`.
- **push-back** — guild's one-time refusal to believe "done without changes" or "blocked".
- **guild branch** — `guild/<task-id>-<slug>`; the only place guild commits.
