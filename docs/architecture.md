# System Architecture

## Layout

```
kokomoor-platform/
├── core/               Shared library (config, db, llm, browser, observability, notifications)
├── pipelines/          Self-contained automation pipelines
│   ├── job_agent/      Pipeline 1: job search automation
│   └── scraper/        Pipeline 2: universal profile-driven web scraper
├── alembic/            Database migrations (shared across all pipelines)
├── docs/               Architecture docs, decisions, glossary
├── scripts/            One-off setup scripts
├── .github/workflows/  CI (ruff + mypy + pytest)
└── docker-compose.yml  Container orchestration
```

## Package boundaries

| Package | Role | Rule |
|---------|------|------|
| `core/` | Shared infrastructure library | Never add pipeline-specific logic. All pipelines import from here. |
| `core/llm/` | LLM abstraction layer | Pipeline code depends on `LLMClient` protocol only. Provider imports (`anthropic`, etc.) stay inside implementation files. |
| `core/browser/` | Managed Playwright with stealth | All browser interactions go through `BrowserManager`. Never use raw Playwright. |
| `core/scraper/` | Shared scraper infrastructure | Dedup engine, content store, HTTP client, fixture management. Domain-agnostic. |
| `core/web_agent/` | LLM-driven web agent | Observe-decide-act loop for autonomous browser navigation. |
| `core/models/` | Shared base models | Only generic bases (`TimestampMixin`, `BaseModel`, `PipelineRun`). Pipeline-specific models belong in the pipeline. |
| `pipelines/<name>/` | Self-contained pipeline | Own models, nodes, state, tests, prompts. Imports from `core/` only. |

## Data flow

```
Settings (core/config.py)
    ↓
Database (core/database.py)         → AsyncEngine + async_sessionmaker → SQLite (Postgres-ready)
LLM (core/llm/)                     → LLMClient protocol → AnthropicClient (or future providers)
Browser (core/browser/)             → BrowserManager → Playwright + stealth + rate limiting
Fetch (core/fetch/)                 → HttpFetcher + BrowserFetcher → shared HTML transport
Scraper (core/scraper/)             → DedupEngine + ContentStore + StealthHttpClient + FixtureStore
Web Agent (core/web_agent/)         → WebAgentController → LLM-driven observe-decide-act loop
Observability (core/observability/) → structlog (JSON/console) + Prometheus metrics
Notifications (core/notifications/) → Async SMTP + IMAP reply watcher (heal triggers)
```

## LLM abstraction

```
LLMClient (Protocol)          ← pipelines depend on this
    ├── AnthropicClient        ← production implementation
    ├── MockLLMClient          ← testing (in core/testing/)
    └── [future providers]     ← implement the protocol, re-export from __init__.py
```

All providers share `LLMUsage` for cost/token tracking. See `core/llm/AGENTS.md` for implementation rules.

## Database

- **SQLModel** for models (Pydantic + SQLAlchemy hybrid).
- **async_sessionmaker** with **AsyncEngine** — always async.
- **SQLite** default (file at `data/platform.db`), **Postgres-ready** via connection string swap.
- **Alembic** for migrations. `alembic/versions/` currently empty (`.gitkeep`). First real migration pending.

## Pipeline pattern

Every pipeline follows this structure:

```
pipelines/<name>/
├── __init__.py
├── __main__.py          Entry point: python -m pipelines.<name>
├── graph.py             LangGraph state machine
├── state.py             Typed state dataclass
├── models/              SQLModel + Pydantic models
├── nodes/               Pure functions: (state) -> state
│   ├── discovery.py     Orchestrates providers, dedup, prefilter
│   ├── bulk_extraction.py  Fetches full job descriptions post-filtering
│   ├── job_analysis.py  LLM-based JD analysis
│   └── ...
├── discovery/           [job_agent] Browser + HTTP provider subsystem
│   ├── models.py        ListingRef, DiscoveryConfig, ProviderResult
│   ├── orchestrator.py  Fan-out to providers, aggregate results
│   ├── providers/       Per-board adapters (LinkedIn, Indeed, Greenhouse, etc.)
│   ├── session.py       Playwright storage_state persistence
│   ├── rate_limiter.py  Per-domain rate limiting
│   ├── human_behavior.py  Realistic mouse/scroll/type simulation
│   ├── captcha.py       Detection + tiered response
│   ├── deduplication.py In-run + DB dedup
│   ├── prefilter.py     Rule-based fit scoring
│   └── url_utils.py     URL canonicalization
├── extraction/          Layered HTML scraping for full page content
├── resume/              [job_agent] Tailoring subsystem: profile, applier, renderer
├── cover_letter/        [job_agent] Cover letter subsystem
├── prompts/             Markdown prompt templates
├── tests/               Unit tests with mocked externals
├── context/             Pipeline-specific reference data (gitignored)
└── Dockerfile
```

## Context folder

The root `/context/` directory is **gitignored** — local-only reference materials (resumes, cover letters, pitch decks, transcripts). Under `pipelines/job_agent/context/`, real tailoring inputs (including `candidate_profile.yaml`) are **gitignored**; only `candidate_profile.example.yaml` is committed as a schema template. Copy the example to `candidate_profile.yaml` locally. See `pipelines/job_agent/AGENTS.md` for the full inventory.

## Job agent data flow

```
Discovery (browser/HTTP providers) -> Filtering -> Bulk Extraction (description fetch)
  -> Job Analysis (LLM) -> Tailoring (LLM) -> Cover Letter Tailoring (LLM)
  -> Tracking -> Notification
```

Manual flow: `Manual Extraction (URL) -> Job Analysis -> Tailoring -> Cover Letter Tailoring -> Tracking -> Notification`

## Discovery architecture

The discovery subsystem uses two provider tiers:

**Browser tier** (Playwright + anti-detection): LinkedIn, Indeed, Built In, Wellfound, Workday, direct career sites. Each provider runs in an isolated `BrowserManager` context with a persisted session (`data/sessions/<provider>.json`, gitignored). All page interactions use `HumanBehavior` for realistic timing and mouse movement. `DomainRateLimiter` enforces per-provider delays. `CaptchaHandler` detects and responds to CAPTCHA challenges.

**HTTP tier** (httpx, no browser): Greenhouse and Lever public JSON APIs. No anti-detection required. Runs concurrently without semaphore limits.

The `DiscoveryOrchestrator` coordinates both tiers with `asyncio.gather()`. Browser providers share an `asyncio.Semaphore(max_concurrent_providers)` to limit simultaneous Playwright contexts.

The `bulk_extraction_node` runs after filtering to fetch full job descriptions. Discovery emits minimal metadata (title, company, URL, salary hint) -- description fetch is deferred to after filtering has reduced the listing count.

## Configuration

All settings via `KP_`-prefixed environment variables, loaded by Pydantic Settings from `.env`. See `.env.example` for the full list. Add new settings to `core/config.py` → `Settings` class.

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs on push/PR to `main`: ruff (check + format), mypy, and pytest with coverage. See root `AGENTS.md` for the exact commands.
