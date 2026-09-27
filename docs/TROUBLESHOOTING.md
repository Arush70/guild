# Troubleshooting

**`guild` is not recognised** — the venv isn't active. `.venv\Scripts\Activate.ps1` (Windows)
or `source .venv/bin/activate`. You should see `(.venv)` in the prompt.

**`doctor` says "ollama not reachable"** — Ollama isn't running. Check the tray icon / `ollama list`.
If it runs on another machine or port, set `OLLAMA_HOST=http://host:11434`.

**"not pulled — ollama pull X"** — run that command. `guild init` picks models that fit your GPU;
if a model was recommended that is too slow, edit `.guild/profiles/<name>.yaml`.

**Every candidate for slot failed** — nothing in that slot's chain is reachable. `guild doctor`
shows why per model (missing key, not pulled, unreachable). Fix one of them or add another.

**The engineer "did nothing" / wrote the tool call as text** — guild executes text tool calls
automatically. If you still see no file changes, check the Activity panel for `refused:` lines
(a path outside the project, or a read-only role trying to write) and the trace in
`.guild/runs/<id>/trace.jsonl`.

**Task not accepted, "TESTS FAILED"** — guild ran your `test_command` and it failed. The
failing test names are in the task notes. Either the engineer broke something (it gets
revision rounds to fix it) or your `test_command` is wrong for this project — check
`.guild/config.yaml`.

**"you have uncommitted changes to tracked files; guild will not create a branch"** — commit or
stash first. Guild refuses to commit onto your current branch. Untracked files are fine.

**Lead produces vague tasks ("design X", "document Y")** — the Plan check flags these. Small
local models do this; edit the task in the dashboard, or put a stronger model first in the
`frontier` slot (a free Groq/Gemini key helps a lot; `lite` uses Claude).

**Local model is slow** — a model that doesn't fit VRAM spills into RAM and runs on the CPU.
`guild doctor` prints what fits. On 8 GB VRAM stay at 7–8b models.

**Reply didn't match the JSON contract** — guild asks the model to repair its reply up to twice
(see `note` events in the trace). If a role keeps failing, its model is too weak for that
role; move it to a stronger slot.

**Merge conflict from the dashboard** — guild aborts the merge and leaves both branches
untouched. Merge manually: `git merge guild/t1-...`, resolve, commit.

**Windows: `Expand-Archive` / paths with spaces** — quote the path:
`cd "C:\Machine Learning projects\guild"`.

**Costs** — `guild cost` sums *estimates* from list prices in `providers/router.py`. Your
provider dashboard is the truth. Every profile has `max_usd_per_run`; the free profile is 0
and refuses paid models even if a key is set.
