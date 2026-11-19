# Tasks — codeguild

Work top to bottom. One task per Claude Code session. Each task: implement → `pytest` →
review the diff → commit → tick here and update `docs/MEMORY.md`.

## Phase 1 — Prove the loop on real local models (v1.0 gate)

- [x] TASK-001 Run the demo on `free` (`cd ..\Demo`, `codeguild ui`, goal "Add a multiply(a, b)
      function to app.py and a test_multiply test in test_app.py"). Read
      `.codeguild/runs/<latest>/trace.jsonl`. Record what the 7b Engineer did in `docs/MEMORY.md`.
- [x] TASK-002 Fix whatever TASK-001 exposed (prompt or parsing, not architecture). Add a
      test reproducing the model behaviour. (Guards + tests landed 2026-10-02; the re-run of
      T1 on the real model with them is the first step of TASK-003.)
- [ ] TASK-003 Run the 4-task calculator plan end to end with **run all**; every task accepted
      and merged. Now one command: `codeguild bench -n 1` (writes report.md with time/tokens).
- [ ] TASK-004 Repeat TASK-003 three times from a fresh folder; it must succeed 3/3:
      `codeguild bench -n 3`. The nightly `real-models` workflow does the same on a 3b model.
- [ ] TASK-005 Same demo on `lite` with `ANTHROPIC_API_KEY` set. Confirm cost stays under
      `max_usd_per_run` and `codeguild cost` matches the provider dashboard within 20%.
- [ ] TASK-006 Chat quality: ask 5 questions about the demo project; the Assistant must read
      files before answering (check trace for `list_files`/`read_file`). Tune the prompt if not.

## Phase 2 — Release

- [x] TASK-010 PyPI name: the whole project is `codeguild` (ADR-021; `guild-ai` was too close to
      the existing `guildai`). `python -m build` and `twine check` pass.
- [ ] TASK-011 (passes on Linux as of 2026-10-02; confirm on Windows) `python -m build`, install the wheel in a clean venv, run `codeguild init -y` and
      `codeguild doctor` in a temp folder. Fix any missing package data.
- [ ] TASK-012 Publish `0.3.0` to TestPyPI, then PyPI. Tag `v1.0.0` once Phase 1 is 3/3.
- [ ] TASK-013 README: replace the screenshot with one from a real accepted run; add a 30-second
      GIF of plan → run → merge.

## Phase 3 — Product depth

- [ ] TASK-020 `codeguild init --docs`: scaffold `docs/PRD.md`, `docs/ARCHITECTURE.md`, `RULES.md`,
      `TASKS.md`, `docs/MEMORY.md` templates in a project (done in 0.2.x — verify on Windows).
- [x] TASK-021 Any role with a `review:` line in `roles_enabled` reviews every task (2026-11-18).
- [ ] TASK-022 Per-task git worktrees so two engineers can run in parallel on independent tasks.
- [x] TASK-023 `ux` and `data-quality` reviewer roles (2026-11-18).
- [x] TASK-024 Nightly real-model job: `.github/workflows/real-models.yml` runs `codeguild bench`
      on Ollama; non-blocking; report on the run's summary page (2026-11-18).
- [x] TASK-025 Chat history persists in `.codeguild/chat.jsonl` (done with TASK-030).

## Phase 4 — IDE

- [x] TASK-040 VS Code extension (`vscode-extension/`): starts `codeguild ui`, plan tree, run /
      review / merge / discard, chat + live activity view, status bar. `npm test` there.
      Still to do by hand: install the `.vsix` in real VS Code on Windows and click through
      it once (the automated test uses a stand-in for the `vscode` module).
- [x] TASK-041 IDE-style web dashboard with a vendored Monaco editor (ADR-022), replacing the
      old single-page dashboard.
- [x] TASK-042 `POST /api/cancel`: stop any job without restarting the server (dashboard *stop*
      button, extension "Stop Current Job", Ctrl+C in `codeguild pair`).
- [x] TASK-030 Pair mode (ADR-016): `pair` role, approval gate with diff preview, modes
      ask / auto-edits / plan, `codeguild pair` in the CLI, `/api/pair` + `/api/approve/{id}`,
      approval UI in the dashboard and the VS Code extension. Also done with it: chat history
      persists in `.codeguild/chat.jsonl` (was TASK-025).
      Still to do: try it on a real model (free: 7b in the `frontier` slot will be weak at this;
      `lite` is the realistic target) and tune `data/roles/pair.yaml` from the trace.
- [ ] TASK-031 Pair/chat context: `@path` attaches files; keep tool results across turns (today
      only the text of earlier turns is replayed); compact long histories with the cheap slot.
- [ ] TASK-034 Slash commands (`.codeguild/commands/<name>.md`) and write hooks.
- [x] TASK-047 A model per role in any profile, own profiles for all projects, team on/off per
      role: `codeguild team`, `codeguild profile`, dashboard Team and models (ADR-023).
- [x] TASK-046 Close the audit's open items: regex timeout, `codeguild trust`, pinned web_fetch,
      model ids, Windows CI (2026-11-13, ADR-020). Owner: check the first Windows CI run.
- [x] TASK-045 Break-it audit 2 after the refactor (2026-11-12, docs/MEMORY.md, ADR-018/019).
- [x] TASK-044 Untangle refactor of the whole codebase (2026-11-12, `docs/UNTANGLE.md`,
      ADR-017). Follow-up for the owner: run the suites on Windows and one real-model task.
- [ ] TASK-043 Publish the extension (VS Code Marketplace / Open VSX) once TASK-012 is done;
      the `arush70` publisher id in `vscode-extension/package.json` must exist there first.

## Done

- [x] 0.1.0 core loop, roles, profiles, CLI, traces, sandbox
- [x] 0.2.0 dashboard, streaming, validation/repair, init wizard, kaggle/performance, watch
- [x] 0.2.x project picker, files, chat, revise plan, push-backs, dependency gating, git init offer
