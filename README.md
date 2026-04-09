# Kokomoor Platform

**An agentic pipeline platform that automates the tedious parts of job searching** — from scraping listings to tailoring resumes — so you can focus on the interviews that matter.

---

### What it does

Drop in a job URL. Get back a tailored `.docx` resume in seconds.

```bash
python scripts/run_manual_url_tailor.py "https://amazon.jobs/en/jobs/3185564/principal-product-manager"
```

The pipeline scrapes the full job page (qualifications, requirements, salary, everything), runs structured LLM analysis to understand what the employer cares about, then generates a precision-tailored resume from your master profile — formatting, section ordering, and bullet selection all handled automatically.

### How it works

```
Job URL ─────► Extraction ─────► Job Analysis (LLM) ─────► Tailoring (LLM) ─────► .docx
               Scrape page        Themes, keywords,         Plan bullet             Styled
               Extract fields     qualifications,           selection,              resume
               Normalize text     domain signals            rewrite, order          document
```

**Extraction** fetches the page (HTTP first, Playwright fallback for JS-rendered sites), detects provider/source from the **resolved final URL** after redirects, parses structured metadata (JSON-LD, Open Graph), runs provider-specific selectors (LinkedIn, Greenhouse, Lever, Workday, Amazon, etc.), and falls back to generic content-block scoring. Structured metadata is treated as one candidate, not an automatic winner: visible rich sections (responsibilities/qualifications) can win when higher quality.

**Job Analysis** sends the complete description to an LLM (Haiku, configurable) for structured extraction: themes, seniority level, domain tags, ATS keywords, basic/preferred qualifications, and positioning angles.

**Tailoring** takes the analysis + your master profile (a YAML file with every possible resume bullet, tagged by domain) and generates a tailoring plan — which bullets to keep, shorten, or rewrite for this specific role. A deterministic applier assembles the final document, and a code-based renderer produces a formatted `.docx`.

### Example use cases

- **Active job search**: Feed URLs from job boards as you find interesting listings. Each produces a role-specific resume in `data/tailored_resumes/`.
- **Pipeline automation**: The full graph (discovery → filtering → analysis → tailoring → tracking → notification) runs end-to-end when the discovery node is implemented with board-specific scrapers.
- **Portfolio artifact**: The codebase demonstrates production patterns — LangGraph orchestration, structured LLM outputs, anti-detection browser automation, typed configuration, and comprehensive testing.

---

## Architecture

```
kokomoor-platform/
├── core/                    Shared infrastructure (config, db, llm, browser, fetch, observability)
│   ├── config.py            Pydantic Settings — typed, env-aware configuration
│   ├── database.py          Async SQLModel engine + session factory
│   ├── llm/                 LLMClient protocol, AnthropicClient, structured_complete
│   ├── browser/             Playwright lifecycle: stealth, rate limiting, fingerprinting
│   ├── fetch/               Shared HTML transport: HttpFetcher, BrowserFetcher, JSON-LD parsing
│   ├── observability/       structlog + Prometheus metrics
│   └── notifications/       Async SMTP email
│
├── pipelines/
│   └── job_agent/           Pipeline 1: Job Application Agent
│       ├── graph.py         LangGraph state machine (full + manual flows)
│       ├── state.py         Typed pipeline state
│       ├── models/          JobListing (SQLModel), SearchCriteria, resume tailoring models
│       ├── nodes/           Discovery → Filtering → Job Analysis → Tailoring → Tracking → Notification
│       ├── extraction/      Layered HTML scraping: JSON-LD, provider selectors, generic scoring
│       ├── resume/          Profile loading, plan application, .docx rendering
│       ├── prompts/         Version-controlled LLM prompt templates
│       └── tests/           Node-level tests with mocked LLM, no API calls
│
├── scripts/                 Manual pipeline entry points
├── alembic/                 Database migrations (shared)
└── .github/workflows/       CI: ruff + mypy + pytest
```

### Design principles

- **`core/` is a library, not a service.** Pipelines import from it. No microservice overhead.
- **Each pipeline is self-contained.** Own models, nodes, tests. Can be developed independently while sharing infrastructure.
- **Two LLM passes, not one.** Job analysis (cheap model, full JD) and tailoring plan (capable model, filtered profile) run in separate graph nodes. Facts live in your profile YAML, not in LLM output. Layout is owned by code, not negotiated with the model.
- **Anti-detection is first-class.** Browser automation uses randomized fingerprints, rate limiting, and realistic timing through `BrowserManager`.
- **Observability from day one.** Every LLM call is traced (LangSmith), costed, and logged (structlog).

## Stack

| Layer | Choice |
|-------|--------|
| Language | Python 3.11+ |
| LLM | Anthropic API (Claude Haiku + Sonnet) |
| Orchestration | LangGraph |
| Browser | Playwright (async, stealth) |
| Data | SQLModel + SQLite (Postgres-ready) |
| Documents | python-docx |
| Validation | Pydantic v2 |
| Observability | structlog, LangSmith, Prometheus |
| CI/CD | GitHub Actions |
| Linting | ruff, mypy (strict) |

## Quick start

```bash
git clone https://github.com/kokomoor/kokomoor-platform.git
cd kokomoor-platform

cp .env.example .env
# Add your Anthropic API key to .env

python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium

# Run against a job URL
python scripts/run_manual_url_tailor.py "https://company.com/careers/job-123"
```

### Outputs

The script writes three files to `data/tailored_resumes/<run-id>/`:

| File | Purpose |
|------|---------|
| `*.docx` | Tailored resume, ready to submit |
| `extracted_job_*.md` | Full scraped job description (verify what was captured) |
| `job_analysis_*.md` | Structured LLM analysis (verify what the tailoring sees) |

`run-id` defaults to a unique timestamp+URL hash value (`manual-url-...`) and can be overridden with a second CLI argument or `KP_MANUAL_RUN_ID`.

## Development

```bash
# Lint + format
ruff check core/ pipelines/
ruff format core/ pipelines/

# Type check
mypy core/ pipelines/ --ignore-missing-imports

# Test (80 tests, ~2s, no API calls)
pytest -v
```

All three must pass before merge. CI enforces this on every PR to `main`.

## Configuration

All settings use `KP_`-prefixed environment variables. See [`.env.example`](.env.example) for the full list.

Key settings:

| Variable | Default | Purpose |
|----------|---------|---------|
| `KP_ANTHROPIC_API_KEY` | (required) | Anthropic API key |
| `KP_JOB_ANALYSIS_MODEL` | `claude-haiku-4-5-20251001` | Model for job analysis (cheap structured extraction) |
| `KP_RESUME_PLAN_MODEL` | `""` (= Sonnet) | Model for tailoring plan (empty = default model) |
| `KP_JOB_ANALYSIS_MAX_INPUT_CHARS` | `30000` | Safety cap on JD length sent to analysis LLM |
| `KP_FETCH_BROWSER_TIMEOUT_MS` | `20000` | Browser navigation timeout for `BrowserFetcher` |
| `KP_RESUME_MASTER_PROFILE_PATH` | `pipelines/.../candidate_profile.yaml` | Path to your master resume profile |

Job-analysis caching is keyed by `dedup_key + description hash`, so re-scraping the same URL with updated JD content will trigger a fresh analysis.

## Future pipelines

The platform is designed for multiple pipelines sharing `core/`:

- **ML Showcase** — Automated ML project generation + GitHub upload
- **Portfolio Manager** — GitHub profile and repo maintenance

Each follows the same pattern: folder under `pipelines/`, imports from `core/`, own tests and Dockerfile.

## License

MIT
