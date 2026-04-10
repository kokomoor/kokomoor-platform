# AGENTS.md — Kokomoor Platform

Personal agentic pipeline platform. Shared infrastructure (`core/`) powers self-contained automation pipelines (`pipelines/`). Current pipelines: job search automation and universal web scraping.

## Read first

1. `docs/product-vision.md` — what this is, who it's for, hard constraints
2. `docs/architecture.md` — system layout, package boundaries, data flow
3. `docs/decisions.md` — why key choices were made
4. `docs/glossary.md` — domain and codebase terminology

## Repo structure

```
core/           Shared library: config, database, LLM, browser, observability, notifications
pipelines/      Self-contained pipelines (each has own models, nodes, tests, Dockerfile)
  job_agent/    Pipeline 1: job search automation
    discovery/  Browser + HTTP providers, session persistence, rate limiting, dedup, prefilter
    nodes/      Pipeline nodes: discovery, bulk_extraction, filtering, job_analysis, tailoring, ...
  scraper/      Pipeline 2: universal profile-driven web scraper
    wrappers/   Site-specific wrappers on top of BaseSiteWrapper
    nodes/      scrape, validate, onboard, heal
alembic/        Database migrations (shared)
data/sessions/  Browser session storage (gitignored)
docs/           Architecture docs, decisions, glossary
scripts/        Setup scripts
.github/        CI workflow
```

## How to validate changes

Every change must pass all three before merge:

```bash
ruff check core/ pipelines/
ruff format --check core/ pipelines/
mypy core/ pipelines/ --ignore-missing-imports
pytest
```

CI (`.github/workflows/ci.yml`) enforces this on all PRs to `main`.

## Key invariants

- **`core/` is a library.** Pipelines import from it. Never add pipeline-specific logic to `core/`.
- **Each pipeline is self-contained.** Own models, nodes, state, tests, prompts. Imports only from `core/`.
- **Never auto-submit applications.** The job agent must pause for human approval before any submission.
- **All browser automation goes through `BrowserManager`.** Stealth and rate limiting are mandatory.
- **Browser sessions are gitignored and human-simulated.** All providers use `BrowserManager` with stored sessions. Direct Playwright usage outside of `BrowserManager` is forbidden. Human behavior simulation (`HumanBehavior` class) is mandatory for all interactive browser actions in discovery.
- **LLM calls go through `LLMClient` protocol.** Pipeline code never imports provider SDKs directly.
- **All config via `KP_*` env vars.** Add new settings to `core/config.py`. See `.env.example`.

## Working norms

- Run `ruff format core/ pipelines/` before committing.
- Use `structlog.get_logger(__name__)` for all logging. Never `print()` or stdlib `logging` directly.
- Type all public function signatures. mypy strict is enabled.
- Tests use `MockLLMClient` and in-memory SQLite — no real API calls.

## Scoped guidance

These directories have their own `AGENTS.md` with local rules:

- `core/AGENTS.md` — shared infrastructure rules
- `core/browser/AGENTS.md` — browser stealth stack, session persistence, `BrowserManager` rules
- `core/llm/AGENTS.md` — LLM abstraction and provider implementation
- `core/scraper/AGENTS.md` — shared scraper primitives (dedup, fixtures, content store, HTTP client)
- `core/web_agent/AGENTS.md` — LLM-driven web agent loop
- `pipelines/job_agent/AGENTS.md` — job pipeline domain, nodes, and constraints
- `pipelines/job_agent/discovery/AGENTS.md` — discovery subsystem contract, provider checklist
- `pipelines/scraper/AGENTS.md` — universal scraper pipeline contract
