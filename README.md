# Kokomoor Platform

> Built this to solve my own job search. Ended up being a more interesting engineering problem than most things I get paid for.

**An agentic pipeline platform that automates the tedious parts of job searching** — from scraping listings to tailoring resumes — so you can focus on the interviews that matter.

---

### What it does

Drop in a job URL. The pipeline scrapes the full job page, runs structured LLM analysis to extract what the employer actually cares about, then generates a precision-tailored resume from a structured master profile — bullet selection, section ordering, and formatting all handled in code, not negotiated with the model.

The interesting engineering is in the seams: anti-detection browser automation that survives LinkedIn's bot detection, a two-pass LLM architecture that separates cheap fact-extraction from expensive reasoning, and a deterministic document renderer that keeps layout out of the model's hands entirely.

```bash
python scripts/run_manual_url_tailor.py "https://amazon.jobs/en/jobs/3185564/principal-product-manager"
```

### How it works

**Automated discovery flow:**

```
Discovery ──► Filtering ──► Bulk Extraction ──► Job Analysis (LLM) ──► Tailoring (LLM) ──► .docx
Scrape boards  Salary/role   Fetch full page    Themes, keywords,      Plan bullet          Styled
LinkedIn       filters       content (after     qualifications,        selection,           resume +
Indeed                       filtering reduces   domain signals         rewrite, order       cover letter
Greenhouse                   the set)
Lever, etc.
```

**Manual single-URL flow:**

```
Job URL ──► Extraction ──► Job Analysis (LLM) ──► Tailoring (LLM) ──► .docx
            Scrape page    Themes, keywords,       Plan bullet          Styled
            Extract fields qualifications,         selection,           resume
            Normalize text domain signals          rewrite, order       document
```

**Extraction** fetches the page (HTTP first, Playwright fallback for JS-rendered sites), detects provider/source from the **resolved final URL** after redirects, parses structured metadata (JSON-LD, Open Graph), runs provider-specific selectors (LinkedIn, Greenhouse, Lever, Workday, Amazon, etc.), and falls back to generic content-block scoring. Structured metadata is treated as one candidate, not an automatic winner: visible rich sections (responsibilities/qualifications) can win when higher quality.

**Job Analysis** sends the complete description to an LLM (Haiku, configurable) for structured extraction: themes, seniority level, domain tags, ATS keywords, basic/preferred qualifications, and positioning angles.

**Tailoring** takes the analysis + your master profile (a YAML file with every possible resume bullet, tagged by domain) and generates a tailoring plan — which bullets to keep, shorten, or rewrite for this specific role. A deterministic applier assembles the final document, and a code-based renderer produces a formatted `.docx`.

### Example use cases

- **Portfolio artifact**: The codebase demonstrates production patterns — LangGraph orchestration, structured LLM outputs, anti-detection browser automation, typed configuration, and comprehensive testing across 270+ tests with no API calls.
- **Active job search**: Feed URLs from job boards as you find interesting listings. Each produces a role-specific resume in `data/tailored_resumes/`.
- **Pipeline automation**: The full graph (discovery → filtering → analysis → tailoring → tracking → notification) runs end-to-end when the discovery node is implemented with board-specific scrapers.

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
│   └── notifications/       Async SMTP email + IMAP heal reply watcher
│
├── pipelines/
│   ├── job_agent/           Pipeline 1: Job Application Agent
│       ├── graph.py         LangGraph state machine (full + manual flows)
│       ├── state.py         Typed pipeline state
│       ├── models/          JobListing (SQLModel), SearchCriteria, resume tailoring models
│       ├── nodes/           Discovery → Filtering → Bulk Extraction → Job Analysis → Tailoring → ...
│       ├── discovery/       Browser + HTTP provider subsystem (LinkedIn, Indeed, Greenhouse, ...)
│       ├── extraction/      Layered HTML scraping: JSON-LD, provider selectors, generic scoring
│       ├── resume/          Profile loading, plan application, .docx rendering
│       ├── cover_letter/    Cover letter generation, validation, .docx rendering
│       ├── prompts/         Version-controlled LLM prompt templates
│       └── tests/           Node-level tests with mocked LLM, no API calls
│   └── scraper/             Pipeline 2: Universal Scraper
│       ├── nodes/           scrape, validate, onboard, heal
│       ├── wrappers/        Base wrapper + site-specific adapters
│       ├── profiles/        SiteProfile YAML configs
│       └── tests/           Offline fixture + contract + scale tests
│
├── scripts/                 Manual pipeline entry points
├── alembic/                 Database migrations (shared)
└── .github/workflows/       CI: ruff + mypy + pytest
```

### Design principles

- **`core/` is a library, not a service.** Pipelines import from it. No microservice overhead.
- **Each pipeline is self-contained.** Own models, nodes, tests. Can be developed independently while sharing infrastructure.
- **Two LLM passes, not one.** Job analysis (cheap model, full JD) and tailoring plan (capable model, filtered profile) run in separate graph nodes. Facts live in your profile YAML, not in LLM output.
- **No layout in the model.** The LLM produces a tailoring plan — which bullets to keep, shorten, or rewrite. A deterministic applier executes it. A code-based renderer formats the document. The model never touches the .docx directly.
- **Anti-detection is first-class.** Browser automation uses randomized fingerprints, rate limiting, and realistic timing through `BrowserManager`.
- **Observability from day one.** Every LLM call is traced (LangSmith), costed, and logged (structlog).

## Why I built this

I'm finishing my MBA at MIT Sloan and running a job search in deeptech — defense, AI infrastructure, advanced energy. The problem I kept running into: tailoring a resume to a specific JD is genuinely tedious work, and the tedious work compounds across 30+ applications.

So I automated it. The platform pulls job listings, analyzes them against a structured master profile (`candidate_profile.yaml`), and generates tailored resumes and cover letters. I use it for my own search. It works.

The secondary goal was to build something production-grade enough to show: LangGraph orchestration, anti-detection browser automation, structured LLM outputs, typed configuration, comprehensive testing. All the patterns I'd want to see in a codebase I was inheriting.

## Stack

| Layer | Choice |
|-------|--------|
| Language | Python 3.12+ |
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

# Test (~270 tests, no API calls)
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

## Discovery configuration

The discovery subsystem scrapes job boards and public APIs to find relevant listings. Configure providers via `KP_*` environment variables in `.env`:

**Provider enable flags:**

| Variable | Default | Provider |
|----------|---------|----------|
| `KP_DISCOVERY_LINKEDIN_ENABLED` | `true` | LinkedIn (browser, requires credentials) |
| `KP_DISCOVERY_INDEED_ENABLED` | `true` | Indeed (browser, no auth) |
| `KP_DISCOVERY_BUILTIN_ENABLED` | `true` | Built In (browser, no auth) |
| `KP_DISCOVERY_WELLFOUND_ENABLED` | `false` | Wellfound (browser, requires login) |
| `KP_DISCOVERY_GREENHOUSE_ENABLED` | `true` | Greenhouse (HTTP API, no auth) |
| `KP_DISCOVERY_LEVER_ENABLED` | `true` | Lever (HTTP API, no auth) |
| `KP_DISCOVERY_WORKDAY_ENABLED` | `false` | Workday (browser, requires target list) |

**Session persistence:** Browser sessions are stored in `data/sessions/` (gitignored). Established sessions dramatically reduce bot detection risk. LinkedIn sessions need 48-72h of warmup before reliable operation — the first run may require manual CAPTCHA or email verification.

**LinkedIn setup:** Set `KP_LINKEDIN_EMAIL` and `KP_LINKEDIN_PASSWORD`. The first run will create a session file. After manual verification (if prompted), subsequent runs reuse the stored session.

**Greenhouse / Lever:** Set comma-separated company slugs: `KP_GREENHOUSE_TARGET_COMPANIES=anduril,palantir,scale-ai` and `KP_LEVER_TARGET_COMPANIES=openai,figma`. These use public JSON APIs and require no credentials.

**CAPTCHA strategy:** `KP_CAPTCHA_STRATEGY` controls behavior when CAPTCHAs are encountered:
- `avoid` (default behavior: skip the provider)
- `pause_notify` (log warning, notify owner, skip provider for this run)
- `solve` (submit to 2captcha API — requires `KP_CAPTCHA_API_KEY`)

**Concurrency:** `KP_DISCOVERY_MAX_CONCURRENT_PROVIDERS` (default 2) limits simultaneous browser contexts. `KP_DISCOVERY_MAX_PAGES_PER_SEARCH` (default 8) caps pagination depth per search URL.

## Future pipelines

The platform is designed for multiple pipelines sharing `core/`:

- **ML Showcase** — Automated ML project generation + GitHub upload
- **Portfolio Manager** — GitHub profile and repo maintenance

Each follows the same pattern: folder under `pipelines/`, imports from `core/`, own tests and Dockerfile.

## Universal scraper quick run

Run a live smoke test for any configured scraper site profile:

```bash
python -m pipelines.scraper.smoke_test --site-id indeed --query "software engineer" --location "Boston, MA"
```

Key scraper settings:
- `KP_SCRAPER_PROFILES_DIR`
- `KP_SCRAPER_FIXTURES_DIR`
- `KP_SCRAPER_CONTENT_DIR`
- `KP_SCRAPER_DEDUP_DB_PATH`

Heal reply watcher settings:
- `KP_IMAP_HOST`, `KP_IMAP_USERNAME`, `KP_IMAP_PASSWORD`
- `KP_HEAL_TRIGGER_SIGNING_SECRET`
- `KP_HEAL_TRIGGER_TOKEN_TTL_S`

## License

MIT
