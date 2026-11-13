# Security Requirements — codeguild

codeguild runs LLM-generated actions against a developer's own machine. The threat model is:
an untrusted model (or a prompt-injected file in the repo) tries to read secrets, escape
the project, run destructive commands, or slip bad code into `main`.

## Filesystem

- All tool paths resolve inside the project root; `..` and absolute escapes are refused
  (`ToolContext.resolve`). Symlinks are resolved before the check.
- Ignore rules hide `.git`, `.codeguild`, `.venv`, `node_modules` etc. from listing/search.
- Reviewer roles (critic, security, performance, verifier, assistant) are read-only:
  no `write_file`/`edit_file`, and `run_command` refuses commands that look mutating.
- The docs role may only write `*.md`.
- No role can write into `.git/` (hooks and config are executed by git on the host) or
  `.codeguild/` (codeguild's own config, roles, plan, traces).
- Agents cannot read `.env` / `.env.*` files (`.env.example` is allowed); `search` skips them
  and never follows a symlink that points out of the project.
- An agent can only call the tools listed in its role YAML, whatever tool name the model emits.
- The read-only command allowlist refuses chaining (`&`, `;`, newline, `&&`, `||`), redirection,
  substitution and the write/exec options of allowed commands (`find -delete/-exec`,
  `git branch -D`, `sort -o`, `--output=`, `--fix`, …). `git_diff` refuses option-like refs.

## Pair mode (interactive)

- `codeguild pair` / the pair modes of the dashboard and VS Code let an agent change the working
  tree directly (no branch). Every file change and every command outside the inspection
  allowlist is shown to the owner first and only happens on approval; "approve all edits" lasts
  for one turn (dashboard / VS Code) or one session (CLI) and never covers commands.
- What cannot be shown is not run: a call whose preview fails is refused. Extra arguments
  for `run_tests` beyond plain test-runner arguments are shown as a command. Control
  characters and text-direction overrides in a command or diff are displayed as `\u001b`
  escapes, so the text approved is the text that runs.
- The approval is an additional gate. Everything in "Filesystem" above still applies, and with
  no approver attached the gate declines.
- `plan` mode is read-only.

## Secrets

- Subprocesses (tests, commands) run with `*KEY*`, `*TOKEN*`, `*SECRET*`, `*PASSWORD*`,
  `*CREDENTIAL*`, `*AUTH*`, `*COOKIE*`, `*_PAT`, `*_DSN` env vars removed, and any variable
  whose value is a URL with a password in it, so a model can't `env` its way to your keys.
  (Proxy variables and `XAUTHORITY` are kept: commands need them.)
- Credential files — `.env*` (not `.env.example`), private keys (`id_rsa`, `*.pem`, `*.key`),
  `.netrc`, `.npmrc`, `.pypirc`, `credentials.json`, `secrets.*` — are not read by the file
  tools, under any name Windows opens them by (`.ENV`, `.env.`, `.env::$DATA`). One created
  during a task is left out of the review diff and of codeguild's commit.
- API keys are read from the environment only; never written to config, plan or trace.
- `.env` is git-ignored; `.env.example` documents names only.
- Traces may contain code excerpts and tool output; `.codeguild/runs/` is git-ignored by
  `codeguild init`.

## Git

- codeguild only commits on a `codeguild/<task>` branch it created from a clean tracked tree.
- It never merges, pushes, rebases, resets or deletes branches on its own; the dashboard
  merge/discard are explicit human actions and refuse on a dirty tree or conflicts.
- `.codeguild/` is excluded from codeguild's commits and from review diffs.
- One codeguild process at a time may plan, run tasks or pair in a project (an OS lock on
  `.codeguild/lock`); a second one stops with "… is already working on this project".

## Network

- The dashboard binds to 127.0.0.1 only; there is no auth because there is no remote access.
  It also refuses requests whose `Host` is not localhost and state-changing requests whose
  `Origin` is not the dashboard's own, port included (a web page cannot drive it through
  DNS rebinding or a cross-site POST, and neither can another program's page on localhost).
  Extra host names can be allowed with `CODEGUILD_UI_HOSTS=name1,name2`.
  Do not expose it with a reverse proxy without adding auth.
- Model providers are contacted over HTTPS via the official SDKs; Ollama is local HTTP.
- `web_fetch` (researcher/kaggle/assistant) fetches public http(s) URLs only — localhost,
  private-network and link-local/metadata addresses are refused on every redirect hop — and
  strips scripts;
  fetched content is untrusted data to the model, and the security reviewer role exists
  partly to catch injected instructions that made it into code.

## Spend

- `limits.max_usd_per_run` is enforced before every model call. The `free` profile is $0
  and refuses priced models even if a key is present — before the request is sent.
  `.codeguild/prices.yaml` (`provider/model: [input, output]` per 1M tokens, or `0`) overrides the
  built-in list prices, e.g. to mark a free-tier model as free.
- A cap can only be kept for models with a known price. Under any cap, a hosted model that is
  not in the price table is refused before the request (it used to count as $0 and so
  could spend without limit). Add it to `.codeguild/prices.yaml`. Models served from this machine
  (Ollama, a local gateway) are exempt.
- Token counts from a provider that are not counts (text, null, negative) are taken as 0.
- Prices are estimates from list prices; the provider dashboard is the truth.

## Projects from other people

- Configuration under `.codeguild/` that is tracked by git may have come with the repository.
  Codeguild runs nothing in such a folder until `codeguild trust` has shown you its test
  command, lint command, sandbox and role/profile files, and you agreed. Projects you set up
  yourself with `codeguild init` are trusted from the start. Trust is per folder.
- The VS Code extension does not run in untrusted workspaces.

## Editing in the dashboard

- Saving a file from the editor obeys the agents' write rules (inside the project, never
  `.git` or `.codeguild`), is refused while a job runs, and is refused when the file changed
  on disk after it was opened.

## Inspection commands (read-only roles; pair mode runs these without asking)

- The command line is split the way a shell splits it; options are checked after quotes and
  escapes are removed, so `"--output=x"` or `-del""ete` hide nothing. Variable expansion
  (`$X`, `${X}`, cmd.exe `%X%`), backticks, `^` and line breaks are refused.
- Refused: options that write a file, load a plugin or config, run another program, install
  packages or upload output (`-o`, `--log-file`, `--init-hook`, `-exec`, `--pastebin`, …),
  and `npm audit fix`.
- Refused: arguments that name a credential file (also through a glob or `git show REV:path`),
  an existing path outside the project, the home folder or a network share; recursive
  `grep`/unfiltered `rg` over a folder that contains a credential file.
- Still true: running the project's tests runs the project's code.

## Sandbox
- Commands get no keyboard input, at most 1 MB of output is kept from each end, and Python
  writes no `.pyc` files (a stale one could make a test run the previous version of a file).

- `sandbox: docker` runs test/commands in a throwaway container with the project
  bind-mounted (`.git` and `.codeguild` read-only), `--network none`, 2 GB / 2 CPU limits. Use it
  for untrusted repos. An unknown `sandbox:` value is an error, never a silent "local".
- `sandbox: local` trusts the project (default for your own code).

## Reviewing changes

- The `security` role reviews every task's diff for secrets, injection, unsafe defaults,
  new dependencies, privacy, CI/supply-chain, with a `block` verdict that prevents
  acceptance.
- Humans still read the diff before merging. This is an LLM writing code.

## Reporting

Open a GitHub issue with the `security` label, or email the maintainer. Do not include
secrets in the report.
