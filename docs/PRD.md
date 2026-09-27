# Product Requirements Document — guild

## Product

**guild** — a budget-aware AI engineering team for your codebase. One command (`guild ui` or
`guild plan` / `guild run`) turns a goal into a plan, implements it task by task on git
branches, verifies with real tests, reviews for bugs and security, and hands you a branch to
merge. Every seat is filled by whatever model your budget allows.

## Problem

Solo developers and students want an AI team that builds real software, but:

- Hosted agents cost more than £10–20/month at agentic token volumes.
- Local 7b models are free but unreliable: they write code as prose, invent tool syntax,
  give up on trivial obstacles, and produce vague plans.
- Single-agent tools have no review step, no test discipline, and no cost visibility.
- Existing multi-agent frameworks assume one strong model everywhere and are hard to run.

## Target users

1. **The owner** — an MSc CS student with a gaming laptop (8 GB VRAM), £10–20/month, who
   wants to ship small projects and Kaggle baselines.
2. Developers who want a reviewable, test-gated AI workflow on their own repo.
3. Learners who want to *see* how a plan → implement → verify → review loop works (traces).

## Goal

Make a stranger able to `pip install guild-ai`, point it at a repo, and get an accepted,
merged branch out of it within 15 minutes — on free local models — with no surprises in
cost or safety.

## Core features

1. Roles as YAML (lead, engineer, critic, security, docs, researcher, performance, kaggle,
   assistant) with least-privilege tools.
2. Profiles (free / lite / pro) mapping abstract slots to fallback chains of real models,
   with a per-run cost cap.
3. Task loop: branch → implement → deterministic test run → critic + security review →
   bounded revisions with model escalation → lead sign-off → docs.
4. Dashboard: project picker, plan (create / edit / revise by prompting), live streaming
   activity, files browser, chat assistant, diff/merge, trace viewer, cost.
5. Robustness for small models: text tool-call execution, output-contract validation with
   repair, push-back on false "blocked" / "done with no changes".
6. JSONL trace of everything, for debugging and for the owner's dissertation (MACS).

## MVP (v0.2 — built)

- All of the above, verified with scripted fake models (59 tests) and in a headless browser.
- Run for real on Ollama `qwen2.5-coder:7b`: Lead plans well; Engineer loop not yet accepted
  end-to-end (fixes landed, unconfirmed).

## Out of scope (for v1.0)

- Parallel engineers / worktrees
- VS Code extension
- Hosted / multi-user service, accounts, billing
- Deployment automation (Vercel etc.)
- Fine-tuning or training models
- Windows installer / GUI app (it's a CLI + local web page)

## Success criteria (v1.0 gate)

1. One task accepted end-to-end on the `free` profile with local 7–8b models, on the demo
   project, repeatably (3 runs in a row).
2. The same on `lite` (Claude Lead, local Engineer).
3. The same on two other small projects (one Python, one non-Python test runner).
4. `pip install guild-ai` from PyPI works on a clean machine; `guild init` needs no manual
   YAML editing on an 8 GB GPU laptop.
5. No run ever commits to `main`, exceeds its cost cap, or touches files outside the project.
