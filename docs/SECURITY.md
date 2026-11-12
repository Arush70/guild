# Security Requirements — guild

guild runs LLM-generated actions against a developer's own machine. The threat model is:
an untrusted model (or a prompt-injected file in the repo) tries to read secrets, escape
the project, run destructive commands, or slip bad code into `main`.

## Filesystem

- All tool paths resolve inside the project root; `..` and absolute escapes are refused
  (`ToolContext.resolve`). Symlinks are resolved before the check.
- Ignore rules hide `.git`, `.guild`, `.venv`, `node_modules` etc. from listing/search.
- Reviewer roles (critic, security, performance, verifier, assistant) are read-only:
  no `write_file`/`edit_file`, and `run_command` refuses commands that look mutating.
- The docs role may only write `*.md`.
- No role can write into `.git/` (hooks and config are executed by git on the host) or
  `.guild/` (guild's own config, roles, plan, traces).
- Agents cannot read `.env` / `.env.*` files (`.env.example` is allowed); `search` skips them
  and never follows a symlink that points out of the project.
- An agent can only call the tools listed in its role YAML, whatever tool name the model emits.
- The read-only command allowlist refuses chaining (`&`, `;`, newline, `&&`, `||`), redirection,
  substitution and the write/exec options of allowed commands (`find -delete/-exec`,
  `git branch -D`, `sort -o`, `--output=`, `--fix`, …). `git_diff` refuses option-like refs.

## Pair mode (interactive)

- `guild pair` / the pair modes of the dashboard and VS Code let an agent change the working
  tree directly (no branch). Every file change and every command outside the inspection
  allowlist is shown to the owner first and only happens on approval; "approve all edits" lasts
  for one turn (dashboard / VS Code) or one session (CLI) and never covers commands.
- The approval is an additional gate. Everything in "Filesystem" above still applies, and with
  no approver attached the gate declines.
- `plan` mode is read-only.

## Secrets

- Subprocesses (tests, commands) run with `*KEY*`, `*TOKEN*`, `*SECRET*`, `*PASSWORD*`,
  `*CREDENTIAL*` env vars removed, so a model can't `env` its way to your API keys.
- API keys are read from the environment only; never written to config, plan or trace.
- `.env` is git-ignored; `.env.example` documents names only.
- Traces may contain code excerpts and tool output; `.guild/runs/` is git-ignored by
  `guild init`.

## Git

- guild only commits on a `guild/<task>` branch it created from a clean tracked tree.
- It never merges, pushes, rebases, resets or deletes branches on its own; the dashboard
  merge/discard are explicit human actions and refuse on a dirty tree or conflicts.
- `.guild/` is excluded from guild's commits and from review diffs.

## Network

- The dashboard binds to 127.0.0.1 only; there is no auth because there is no remote access.
  It also refuses requests whose `Host` is not localhost and state-changing requests with a
  foreign `Origin` (a web page cannot drive it through DNS rebinding or a cross-site POST).
  Extra host names can be allowed with `GUILD_UI_HOSTS=name1,name2`.
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
  `.guild/prices.yaml` (`provider/model: [input, output]` per 1M tokens, or `0`) overrides the
  built-in list prices, e.g. to mark a free-tier model as free.
- Prices are estimates from list prices; the provider dashboard is the truth.

## Sandbox

- `sandbox: docker` runs test/commands in a throwaway container with the project
  bind-mounted (`.git` and `.guild` read-only), `--network none`, 2 GB / 2 CPU limits. Use it
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
