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
  Do not expose it with a reverse proxy without adding auth.
- Model providers are contacted over HTTPS via the official SDKs; Ollama is local HTTP.
- `web_fetch` (researcher/kaggle/assistant) fetches http(s) URLs only and strips scripts;
  fetched content is untrusted data to the model, and the security reviewer role exists
  partly to catch injected instructions that made it into code.

## Spend

- `limits.max_usd_per_run` is enforced before every model call. The `free` profile is $0
  and refuses priced models even if a key is present.
- Prices are estimates from list prices; the provider dashboard is the truth.

## Sandbox

- `sandbox: docker` runs test/commands in a throwaway container with the project
  bind-mounted, `--network none`, 2 GB / 2 CPU limits. Use it for untrusted repos.
- `sandbox: local` trusts the project (default for your own code).

## Reviewing changes

- The `security` role reviews every task's diff for secrets, injection, unsafe defaults,
  new dependencies, privacy, CI/supply-chain, with a `block` verdict that prevents
  acceptance.
- Humans still read the diff before merging. This is an LLM writing code.

## Reporting

Open a GitHub issue with the `security` label, or email the maintainer. Do not include
secrets in the report.
