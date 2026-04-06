# System Architecture

## Layout

```
kokomoor-platform/
├── core/               Shared library (config, db, llm, browser, observability, notifications)
├── pipelines/          Self-contained automation pipelines
│   └── job_agent/      Pipeline 1: job search automation
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
| `core/models/` | Shared base models | Only generic bases (`TimestampMixin`, `BaseModel`, `PipelineRun`). Pipeline-specific models belong in the pipeline. |
| `pipelines/<name>/` | Self-contained pipeline | Own models, nodes, state, tests, Dockerfile, prompts. Imports from `core/` only. |

## Data flow

```
Settings (core/config.py)
    ↓
Database (core/database.py)         → AsyncEngine + async_sessionmaker → SQLite (Postgres-ready)
LLM (core/llm/)                     → LLMClient protocol → AnthropicClient (or future providers)
Browser (core/browser/)             → BrowserManager → Playwright + stealth + rate limiting
Observability (core/observability/) → structlog (JSON/console) + Prometheus metrics
Notifications (core/notifications/) → Async SMTP
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
├── prompts/             Markdown prompt templates
├── tests/               Unit tests with mocked externals
├── context/             Pipeline-specific reference data
└── Dockerfile
```

## Context folder

The root `context/` directory is **gitignored** — it contains local-only reference materials (resumes, cover letters, pitch decks, transcripts) used by the job agent's Tailoring node. The structured extract lives in `pipelines/job_agent/context/candidate_profile.yaml` (version-controlled). See `pipelines/job_agent/AGENTS.md` for the full inventory.

## Configuration

All settings via `KP_`-prefixed environment variables, loaded by Pydantic Settings from `.env`. See `.env.example` for the full list. Add new settings to `core/config.py` → `Settings` class.

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs on push/PR to `main`: ruff (check + format), mypy, and pytest with coverage. See root `AGENTS.md` for the exact commands.
