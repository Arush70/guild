# Guild for VS Code

Run [guild](https://github.com/Arush70/guild) — a budget-aware multi-agent coding team — from
the VS Code side bar. VS Code is the IDE (editor, diff, terminal, git); this extension adds the
team: plan a goal, run tasks, watch each role work, review the branch, merge it, and chat about
the project.

The extension is a thin front end. It starts `guild ui --no-browser` for the folder you opened
and talks to it over the same local HTTP API the web dashboard uses. guild itself stays Python.

## Requirements

- VS Code 1.80 or newer
- guild installed with the dashboard extra: `pip install "guild-ai[ui]"` (or, from a clone,
  `pip install -e ".[dev]"`)
- git, and Ollama or an API key for the models in your profile (`guild doctor` tells you)

## Install

From this folder:

```
npm run package            # builds guild-vscode-0.2.0.vsix (uses npx @vscode/vsce)
code --install-extension guild-vscode-0.2.0.vsix
```

Or, to try it without packaging: open this folder in VS Code and press F5.

### Tell it where guild is

VS Code usually does not see a virtual environment you activated in a terminal. If the Guild
view says it could not start, set **Guild: Command** (`guild.command`) to the full path:

```
C:\Machine Learning projects\guild\.venv\Scripts\guild.exe
```

With the setting empty the extension tries, in order: `.venv` / `venv` inside the opened
folder, the interpreter selected in the Python extension (`python -m guild.cli`), then `guild`
on PATH.

## Use

Open a project folder and click the Guild icon in the activity bar.

| Where | What |
|---|---|
| **Plan** view | The task list. Title bar: New Plan, Run Next, Run All, Stop, Refresh; `…` menu: Revise Plan, Switch Profile, Doctor, Show Run Trace, Open Dashboard in Browser. |
| A task | Inline buttons: Run, Review Changes, Merge. Right-click: Discard Branch, Edit, Set Status, Delete. Expand it for description, done-when, branch and notes. |
| **Chat & Activity** view | *Chat*: ask about the project, or switch the selector to a **Pair** mode to have the agent make changes. *Activity*: the live log of what each role does, with the model's output streaming underneath. |
| Status bar | `guild` — idle, or the current phase while a job runs. |
| Output → Guild | The full log, including the server's own output. |

Review Changes lists the files a task changed. Pick one for VS Code's side-by-side diff, or
"Whole change" for the unified diff. Merge and Discard always ask first; guild never merges on
its own.

### Pair mode

The selector above the message box chooses how the chat behaves:

| Mode | The agent can | You approve |
|---|---|---|
| Chat (read-only) | read files, search, run tests | nothing to approve |
| Pair: ask before every change | edit files, run commands | every file change and every command that is not plain inspection |
| Pair: apply edits, ask for commands | the same | commands only |
| Pair: plan only | read and propose a plan | nothing is changed |

When the agent wants to change something, a card appears in the chat and a notification pops
up: **Approve**, **Open Diff** (the file as it is now against the proposal, in VS Code's diff
editor), **Decline**, or **Approve all edits this turn**. Pair mode edits your working tree
directly — no branch and no commit — so use Source Control to review and commit as usual.

A folder that has no `.guild/config.yaml` is not touched until you choose **Set up guild here**
(runs the `guild init` wizard in a terminal) or **Start without the wizard**.

**Stop Current Job** cancels whatever is running (a plan, a task, a chat turn) without
restarting the server; a cancelled task goes back to "todo".

## Settings

| Setting | Default | |
|---|---|---|
| `guild.command` | empty | How to launch guild (path or command line). |
| `guild.autoStart` | `true` | Start when the opened folder already has `.guild/config.yaml`. |
| `guild.port` | `0` | Port of the local server; 0 picks a free one. |
| `guild.profile` | empty | Profile for jobs started from VS Code; empty uses the project's. |
| `guild.notifications` | `true` | Notify when a task finishes or a job fails. |

## How it is built

- Plain JavaScript, no dependencies, no build step: `extension.js` plus `lib/` and `media/`.
- Every request to guild is made from the extension host. The chat view is a webview with a
  `default-src 'none'` content security policy; it only exchanges messages with the extension.
  (guild's server refuses cross-origin requests, so a webview could not call it anyway.)
- The server listens on 127.0.0.1 only and is stopped when the window closes.

## Tests

```
npm test
```

`test/unit.test.js` covers the client, launcher and formatting. `test/e2e.test.js` loads the
real extension code against a stand-in for the `vscode` module, starts a real `guild ui` and a
scripted OpenAI-compatible model, and goes through plan → run → review → merge → chat → stop.
It is skipped when `guild` is not installed. The tests do not launch VS Code itself.
