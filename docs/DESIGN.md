# Design System — guild dashboard

## Style

Dense, calm, developer-tool. Dark by default, light follows the OS. No hero sections, no
marketing, no emoji in the UI except the role status dots. Everything visible at 1440px
without scrolling the page; panels scroll internally.

## Typography

- UI: `system-ui, -apple-system, Segoe UI, Roboto, sans-serif`, 14px / 1.45
- Code, paths, models, logs: `ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`, 12px
- Section titles: 12px uppercase, letter-spacing .8px, muted colour

## Colours (CSS variables in `index.html`)

| token | dark | light | use |
|---|---|---|---|
| `--bg` | #0f1115 | #f5f6f8 | page |
| `--panel` | #171a21 | #ffffff | cards |
| `--panel2` | #1e222b | #f0f2f5 | inputs, task rows, live pane |
| `--line` | #2a2f3a | #dfe3ea | borders |
| `--text` | #e6e8ee | #1a1d24 | body text |
| `--muted` | #8a92a6 | #667085 | secondary text |
| `--accent` | #7aa2f7 | #3b6fd8 | active agent, phases, primary button |
| `--ok` | #9ece6a | #3d8b3d | pass, accepted, done |
| `--warn` | #e0af68 | #b7791f | fallback, changes requested, plan check |
| `--err` | #f7768e | #c8404f | fail, blocked, error |

Never rely on colour alone: every state also has a word (done / blocked / PASS / FAIL).

## Layout

Three columns: **Team + Chat/Files/Ask + Runs** (300px) · **Goal + Plan** (fluid) ·
**Activity + Doctor** (380px). Below 1100px they stack. Cards: 10px radius, 12px padding,
1px `--line` border. Buttons: 6px radius; `.primary` uses `--accent` fill.

## Components

- **Agent row**: dot (idle grey / active pulsing accent / done ok / bad err), name, model id
  in mono, call count.
- **Task card**: checkbox, id in mono, title, status pill, description, `done when`, notes in
  warn colour, action buttons (run / reset / mark done / edit / delete / diff / merge / discard).
- **Activity line**: mono, left border coloured by kind (phase accent, model call blue,
  tool call muted, warn, error, ok).
- **Live pane**: streaming model text with a blinking cursor; cleared on the next call.
- **Modal**: diff viewer (+/− coloured), trace timeline (grid: time / role / text), file
  viewer with line numbers, project picker.

## UX requirements

- Every long action streams progress; nothing is silent for more than ~2 s.
- Destructive actions (merge, discard, delete task) confirm first.
- The current project path is always visible in the header and is the switch control.
- Errors show the traceback in the Activity panel, not just a message.
- Works with keyboard: Enter sends chat / revise; Escape closes modals.
- Empty states say what to do next ("No plan yet. Enter a goal and press plan").
