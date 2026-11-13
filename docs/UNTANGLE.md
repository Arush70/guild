# Untangle log (2026-11-12)

Decision log for the whole-codebase refactor done with the `untangle-codebase` skill. One
question was asked of every piece of code: **should these pieces change together?** Each fix is
written above the problems it removes. Line numbers refer to the code before the refactor
(commit `bdb8416`).

## Fixes and the problems behind them

- **One plan module (`plan.py`): `Task`, `Plan`, every edit to a plan, and `PlanStore` for
  reading and writing `plan.json`.** The server stops handling the plan as raw dicts.
  - `workflow.py:44-190` defines `Task`/`Plan`; `ui/server.py:733-768` edits the same file as
    untyped dicts with its own field list and its own status set (`TASK_STATUSES`, line 289).
    Add a field to `Task` and the server silently ignores it.
  - `ui/server.py:798` (`task["merged"] = True`) and `:819-821` (discard resets) change task
    state by key name, outside the class that defines what those states mean.
  - Two atomic writers for one file: `workflow.py:390-395` and `ui/server.py:300-304`.
  - `ui/server.py:941-950` re-reads `plan.json` three times with a 50 ms sleep, a leftover from
    before the writer was atomic.
  - `workflow.py:363-373` and `:1216-1236` build `Task` objects from the Lead's JSON twice.
  - `cli.py:484-488` reads `plan.json` by hand a third way.
  - `run_tasks` (`workflow.py:1065-1070`), `cli.py:515-523` and `ui/server.py:333-338` each
    check "does this task id exist" with their own error text.

- **One git module (`repo.py`): `Repo` owns every git command; `TaskBranch` owns the git state
  of one task run.** Nothing else shells out to git.
  - `ui/server.py:771-778` builds a `Guild` with `Guild.__new__`, skipping `__init__`, and sets
    four attributes by hand because the git helpers live on the workflow class. Any new
    attribute a git helper reads breaks the dashboard, and nothing says so.
  - `workflow.py:437-439,483-496,533` keep the task's git state in attributes that may not
    exist yet and are read with `getattr(self, "_on_branch", False)`.
  - Four git runners: `workflow.py:404-423`, `tools/registry.py:526-538`, `cli.py:182-195`
    (init) and `cli.py:897-905` (watch).
  - `cli.py:782` and `cli.py:910` call the private `g._git` / `g._git_rc`.
  - The "is the tree dirty" pathspec is written out twice (`workflow.py:455-463`, `:606-614`)
    and a third, different check sits at `:486`.

- **Verification is its own module (`verify.py`).** `Verifier` runs the tests, parses the
  result and knows whether existing tests disappeared.
  - `workflow.py:645-706` (run + parse) and `:771-776,874-890` (baseline and removed-test
    logic) are one concern spread over the class and the middle of the task loop.

- **Every path under `.codeguild/` and every "codeguild-owned" directory name lives in `paths.py`.**
  - `.codeguild/plan.json` is spelled in `workflow.py:388`, `ui/server.py:301,942`, `cli.py:484`;
    `runs/*/trace.jsonl` in `workflow.py:228`, `ui/server.py:835,861`, `cli.py:820`;
    `config.yaml` in `config.py:153,162`, `ui/server.py:414,427,967`.
  - The protected directories are listed in `tools/registry.py:82` and again in
    `sandbox.py` (read-only mounts).
  - The cache directory names appear in `workflow.py:98-108` (commit excludes), `:532` (new
    file diff), `cli.py:294-301` (.gitignore lines) and, incompletely, in the default ignore
    list `config.py:76-87` (no `.pytest_cache`, no `.ruff_cache`).

- **Path scoping is a `Workspace` (root + ignore list), separate from a role's tool
  permissions.** `ToolContext` is a workspace plus permissions.
  - `ui/server.py:487-489` and `:516-518` construct a `ToolContext` with an empty test command
    and no sandbox only to call `resolve()` / `is_ignored()`.
  - Three directory walkers apply the same ignore rules: `list_files`, `search`
    (`tools/registry.py`) and `/api/files` (`ui/server.py:491-509`).
  - `workflow.py:336-339` builds a Lead tool context to list files for the project summary.

- **Command and network policy move out of the tool implementations (`tools/policy.py`).**
  A security rule and a tool change for different reasons. Each tool declares in its `@tool`
  line whether it changes anything (`mutating`) and how to preview that change for approval.
  - `tools/registry.py:415-524` (read-only allowlist), `:85-93` (secret files) and `:611-640`
    (blocked URLs) sit between tool bodies; tests import them as private names
    (`_readonly_allowed`, `_blocked_url`).
  - `_looks_mutating` (`tools/registry.py:518`) is a wrapper "kept for callers/tests" with no
    callers.

- **One job runner in the server.** `_job(kind, profile, work)` creates the `Guild`, runs the
  work, closes it.
  - Six endpoints repeat make-codeguild / try / finally-close (`ui/server.py:551-558,578-588,
    618-623,690-698,705-716,723-728`).
  - `chat` and `pair` check `busy()` themselves before `Hub.start` checks it again
    (`:544,568`), so that a refused message is not left in the history.

- **Model status, Ollama discovery and run summaries are computed once and shown twice.**
  - `cli.py:340-370` and `ui/server.py:633-657` are the same provider-status ladder, including
    the same hard-coded "these three gateways need no key" set.
  - Three copies of "ask Ollama which models it has": `cli.py:395-403`,
    `ui/server.py:953-961`, `hardware.py:148-156`.
  - Three trace aggregations: `cli.py:832-845`, `ui/server.py:841-853`, `:865-888`.

- **A pair session is an object (`PairSession`): the mode and "approve all edits" live in one
  place.**
  - "Approve the remaining edits" is implemented three times: `workflow.py:1151`
    (auto-edits mode), `ui/server.py:242,362,598` (`S.auto_edits`), `cli.py:680,683,697`.
  - `workflow.py:1112-1116` and `:1179-1183` are the same history-to-messages code.

- **`run_tasks` is the only task loop.** The CLI passes hooks for its prompts.
  - `cli.py:528-576` re-implements queueing, dependency checks and "skip done" next to
    `workflow.py:1051-1097`; the two already disagree on what happens after a rejected task.

- **Role behaviour comes from role data.**
  - `workflow.py:286` gives the profile's tool budget to the role *named* `engineer`.
  - `workflow.py:257` repeats the registry's `mutating` flags as a literal set.

- **Special cases removed by the above:** the `Guild.__new__` construction, the plan re-read
  sleep, the `getattr(..., default)` reads, `_looks_mutating`, the server's two dummy tool
  contexts.

## Second round (after an independent review of the result)

A reviewer that had not seen the work read the refactored code and listed 25 remaining
problems. Fixed:

- **A tool result says whether it worked (`ToolResult.ok`); a refusal is an exception.** Before,
  `agent.py` and the trace decided "did the tool fail" by looking for the words `ERROR` /
  `REFUSED` at the start of the returned text, and every tool formatted its own refusals.
- **A verification is a `Verification` object and a task's reviews are `TaskOutcome.reviews`
  (a dict by reviewer name).** Before, verification was a dict whose keys were spelled in
  `verify.py`, `workflow.py`, `cli.py` and the server, and `TaskOutcome` had one attribute per
  reviewer (`critic`, `security`), so a new reviewer meant a new attribute and new branches
  in three files. `REVIEWERS` in `workflow.py` is now the one list.
- **A phase event says which role works on it (`role`).** The dashboard had its own
  phase-to-role table and its own list of role names; both are gone, it uses `ev.role` and
  the role list from `/api/state`.
- **Log lines carry their meaning.** `format.js` `eventLine` returns `{text, cls}`; the chat
  view no longer colours a line by searching its text for `✖`, `FAIL`, `▶`.
- **A fallback is reported with its own level (`fallback`).** Both front ends dropped the
  duplicate line by matching the English words "trying next candidate".
- **`Cancelled` lives in `errors.py`**; the sandbox, router and workflow no longer import it
  from each other. A cancel also stops a Docker run (`docker kill` on a named container);
  before, only local commands were stopped.
- **A completion carries its own cost and latency** (`Completion.usd`, `.latency_s`), set by
  the router. `agent.py` no longer reaches into the router's tracker for the last call.
- **Facts about a provider come from its endpoint entry** (`Endpoint.local`, `.key_optional`,
  `KEY_ENV`): the "$0 means local models only" rule, `codeguild doctor` and the init wizard no
  longer keep their own lists of provider names.
- **`projectdocs.py`** is the one table of project documents: where each may live and which
  template `codeguild init --docs` writes. The reader and the scaffolder had separate lists.
- **`hardware.TIERS`** names the tiers once; an unknown tier is an error instead of silently
  becoming `free`.
- Removed: the router's retry for providers without an `on_token` parameter (all take it), a
  second `$0` check after the call (the first one already refuses), `by_model` cost totals
  (never read), `env_flag`, the extension's unused `clearLog` message.
- Broad `except Exception` blocks narrowed to the errors each call can raise, and each
  remaining silent `except` says why it is silent.

## Special cases kept (the system still needs them)

- `repo.py`: retry of the commit with a `codeguild` identity when git has no user.name/email.
- `paths.py`: short retry of the atomic replace on Windows, where it fails while another
  process has the file open.
- `workflow.py`: one push-back when the engineer says "blocked", one retry when it reports
  "done" without changing a file. Both are requirements of small models, not of the design.
- `READONLY_ROLES`: reviewers stay read-only even when a project overrides their YAML and
  forgets `readonly: true`. It is a safety rule keyed on the role name on purpose.
- `providers/openai_compat.py`: adapting a request after a 400 that names a parameter; the
  streamed tool-call index workaround for Ollama/Gemini.
- `ui/server.py`: token batching (80 ms), SSE keep-alive.
- `config.py`: a role file without `task_budget` gets it when the role is named `engineer`
  (role files older than the field).
- `TaskOutcome.to_dict()` writes one key per reviewer, the shape front ends already read.
- `verifier` in `roles_enabled` is an on/off switch for the test step, although no model plays
  that role. Changing it would change every existing `config.yaml`.

## Third round: does it still behave the same?

A second independent reviewer compared the old and new code path by path, running the same
scenarios against both. The safety model came out unchanged ($0 rule, path scoping,
`.git`/`.codeguild` protection, secret files, read-only allowlist, approval previews; "commit only
on a codeguild branch" is slightly stricter). It found accidental differences, now fixed and pinned
by `tests/test_same_behaviour.py`:

- `codeguild run` printed only the cost table after a rejected task; the outcome panel (tests,
  critic findings, lead notes) is back.
- An engineer that twice answered "done" without changing a file lost its last push-back
  attempt.
- A project's own `engineer.yaml` written before `task_budget` existed lost the profile's
  tool budget. A role file without the line now means "yes for the role named engineer".
- A task started from a codeguild branch could get that branch (even its own) as merge target.
- "Approve and remember" on a *command* approval switched on auto-approval of later edits
  (reachable only by calling the API directly).
- `/api/state` returned an agent's whole message history after an `ask` job.
- Task results sent to front ends had `reviews: {…}` in place of `critic` / `security` keys.
- `codeguild run` said "no runnable tasks left" after the owner declined a task.
- The Lead saw `not run` / missing blocks where it used to see `null` for a skipped
  verification or reviewer; a timed-out Docker command reported the wrong time limit;
  `search` on a single ignored file returned matches.

## What still looks different

Small, and left as they are:

- The dashboard's team strip lists every role the project has (now also `kaggle`, `pair`,
  `performance`), alphabetically. A pair turn lights `pair` instead of `assistant`.
- Cancelling a job stops a command running in the Docker sandbox as well. Docker output is
  stdout and stderr interleaved (as the local sandbox always was), not one after the other.
- The default ignore list also has `.pytest_cache` and `.ruff_cache`.
- `codeguild init` on a machine without a git identity makes the initial commit as `codeguild` and
  says so, where it used to fail with a hint.
- `codeguild ui` in a folder without a config writes the detected test command, as `codeguild init`
  does, not always the pytest default.
- `build_profile` with an unknown tier is an error.
- Wording: some warnings and HTTP error texts (unknown task, busy server, corrupt plan),
  `codeguild doctor` status text. `codeguild cost --last 0` shows no runs (it showed all).
- Trace: warning notes are worded differently; a failed commit is reported to the owner but
  no longer written as a `note`; the engineer's `decision` event carries the status after a
  push-back; verification events always carry `lint_passed` and `removed_tests`.
- After `codeguild run T2` skips T2 for an unmet dependency, it also prints "no runnable tasks
  left".
- A rejected task returns to its start branch even when only `.codeguild/` files are modified
  (matters only if `.codeguild/plan.json` is tracked in git, which `codeguild init` prevents).
- The extension's Stop no longer has a fallback for codeguild servers older than `/api/cancel`.

## Tests

`tests/test_modules.py` covers the new modules through their own interfaces: plan queries and
edits, git (`Repo`, `TaskBranch`), `Verifier`, paths, project documents, provider status and
`Workspace`. `tests/test_policy.py` holds the command and URL policy tests that used to import
private names from the tool registry. `tests/test_same_behaviour.py` pins the differences
listed above. Tests no longer write into the owner's real `~/.codeguild/recent.json`. Totals: 229
Python tests, 22 extension tests.

## Not changed, and why

- **Model names in three places** (`hardware.py` `build_profile`, `providers/router.py`
  `DEFAULT_PRICES`, `data/profiles/*.yaml`). Deriving the wizard's profile from the packaged
  YAML would change what `codeguild init` writes, so it is a product decision, not a refactor.
  The Anthropic ids in those files (`claude-fable-5-1`, `claude-opus-5`, `claude-sonnet-5`,
  `claude-haiku-4-5`) should be checked against the provider's current model list before the
  first `lite` run.
- **Event rendering exists in both front ends** (`ui/index.html` and
  `vscode-extension/lib/format.js`). They are separate programs in separate languages with no
  shared build; the contract between them and the server is the event list in `trace.py` and the `Hub`
  docstring in `ui/server.py`.
- **`agent.py` keeps JSON/tool-call extraction next to the agent loop.** They change for the
  same reason (how a model's reply is read).
