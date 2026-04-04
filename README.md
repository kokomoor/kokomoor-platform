# Kokomoor Platform

**Personal agentic pipeline ecosystem** — shared infrastructure for automated workflows, starting with job search automation.

## Architecture

```
kokomoor-platform/
├── core/                    ← Shared infrastructure (config, db, llm, browser, observability)
│   ├── config.py            Pydantic Settings — typed, env-aware configuration
│   ├── database.py          Async SQLModel engine + session factory (SQLite → Postgres ready)
│   ├── models/              Base models: timestamps, status tracking, pipeline runs
│   ├── llm/                 Anthropic API client: retry, cost tracking, structured outputs
│   ├── browser/             Playwright lifecycle: anti-detection, rate limiting, stealth
│   ├── observability/       structlog + Prometheus metrics
│   └── notifications/       Async SMTP email
│
├── pipelines/
│   └── job_agent/           ← Pipeline 1: Job Application Agent
│       ├── graph.py         LangGraph state machine
│       ├── state.py         Typed pipeline state schema
│       ├── models/          JobListing, Application, SearchCriteria
│       ├── nodes/           Discovery → Filtering → Tailoring → Application → Tracking
│       ├── tools/           Anthropic tool definitions (scraper, file generation)
│       ├── prompts/         Version-controlled prompt templates
│       └── tests/           Node-level tests with mocked externals
│
├── alembic/                 ← Database migrations (shared across all pipelines)
├── .github/workflows/       ← CI: ruff + mypy + pytest on every PR
└── docker-compose.yml       ← Container orchestration
```

### Design Principles

- **`core/` is a library, not a service.** Pipelines import from it directly. No microservice overhead — the complexity is in clean abstractions, not deployment topology.
- **Each pipeline is self-contained.** Own models, nodes, tests, Dockerfile. Can be developed and deployed independently while sharing infrastructure.
- **Observability from day one.** Every LLM call is traced (LangSmith), costed, and logged (structlog). Prometheus metrics for operational visibility.
- **Schema evolution is built in.** Alembic migrations from the first commit. No "we'll add migrations later."
- **Anti-detection is first-class.** Browser automation uses randomized fingerprints, rate limiting, and human-realistic timing.

## Stack

| Layer | Choice |
|-------|--------|
| Language | Python 3.11+ |
| LLM | Anthropic API (Claude) |
| Orchestration | LangGraph |
| Browser | Playwright (async) |
| Data | SQLModel + SQLite (Postgres-ready) |
| Validation | Pydantic v2 |
| Observability | structlog · LangSmith · Prometheus |
| CI/CD | GitHub Actions |
| Containers | Docker + Compose |
| Linting | ruff · mypy (strict) |

## Setup

```bash
# Clone
git clone https://github.com/kokomoor/kokomoor-platform.git
cd kokomoor-platform

# Environment
cp .env.example .env
# Edit .env with your API keys

# Install
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Install Playwright browsers
playwright install chromium

# Verify
ruff check core/ pipelines/
mypy core/ pipelines/ --ignore-missing-imports
pytest -v
```

### Docker

```bash
docker compose up --build
```

## Pipelines

### Job Application Agent (`pipelines/job_agent/`)

Automates the job search workflow:

1. **Discovery** — Scrape job boards for relevant listings
2. **Filtering** — Deduplicate and apply salary/role/keyword filters
3. **Tailoring** — Generate customized resume + cover letter via Claude
4. **Human Review** — Pause for approval before submission
5. **Application** — Form-fill via Playwright
6. **Tracking** — Persist state to database
7. **Notification** — Email digest of pipeline activity

See [`pipelines/job_agent/README.md`](pipelines/job_agent/README.md) for details.

### Future Pipelines

- **ML Showcase** — Automated ML project generation + GitHub upload
- **Portfolio Manager** — GitHub profile and repo maintenance

All future pipelines follow the same pattern: folder under `pipelines/`, imports from `core/`, own tests and Dockerfile.

## Development

```bash
# Lint
ruff check core/ pipelines/
ruff format core/ pipelines/

# Type check
mypy core/ pipelines/ --ignore-missing-imports

# Test
pytest -v
pytest --cov=core --cov-report=term-missing

# Database migration
alembic revision --autogenerate -m "description"
alembic upgrade head
```

## License

MIT
