# Contributing

- Roles and profiles are YAML in `src/guild/data/`. New roles are welcome if they have a
  clear job, a strict JSON output contract, and the minimum tool set.
- New providers: if it speaks the OpenAI chat API, add one line to `ENDPOINTS` in
  `src/guild/providers/openai_compat.py`. Otherwise add an adapter next to
  `anthropic_provider.py` implementing `complete(...) -> Completion`.
- Tests must not call real APIs. Use `FakeProvider` from `tests/conftest.py`.
- Run `ruff check src tests && pytest` before opening a PR.
- Keep the safety model intact: tools stay scoped to the project root, reviewers stay
  read-only, secrets stay out of subprocess environments.
