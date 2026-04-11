# Kokomoor Platform — Pipeline Guide

A technical reference for running, testing, and understanding the job agent pipeline.

---

## Table of Contents

1. [Setup](#1-setup)
2. [Pipeline Architecture](#2-pipeline-architecture)
3. [Node-by-Node Reference](#3-node-by-node-reference)
4. [State Object](#4-state-object)
5. [Database](#5-database)
6. [Running the Pipeline](#6-running-the-pipeline)
7. [Observing Prompt Caching](#7-observing-prompt-caching)
8. [Testing](#8-testing)
9. [Key Settings Reference](#9-key-settings-reference)

---

## 1. Setup

```bash
# Clone and enter
cd kokomoor-platform

# Install dependencies
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Copy and configure environment
cp .env.example .env
# Edit .env — minimum required:
#   KP_ANTHROPIC_API_KEY=sk-ant-...
#   KP_LINKEDIN_EMAIL / KP_LINKEDIN_PASSWORD  (if using LinkedIn)

# Bootstrap the database (creates data/platform.db with all tables)
python -c "import asyncio; from core.database import init_db; asyncio.run(init_db())"

# Or let the pipeline do it automatically on first run (it calls init_db() at startup)
```

---

## 2. Pipeline Architecture

The pipeline is a **LangGraph state machine** — a directed graph of async nodes that pass a shared `JobAgentState` object through each stage. LangGraph manages the execution loop; you interact with it via `graph.ainvoke(initial_state)`.

```
START
  │
  ▼
[discovery]          ← Scrapes LinkedIn, Greenhouse, Lever, Indeed, etc.
  │
  ▼
[filtering]          ← Salary floor, unknown-salary policy
  │
  ├── no listings → [notification] → END
  │
  ▼
[bulk_extraction]    ← Fetches full job page HTML → description text
  │
  ├── all failed  → [notification] → END
  │
  ▼
[job_analysis]       ← Haiku: extracts themes, keywords, requirements (parallel)
  │
  ├── all failed  → [notification] → END
  │
  ▼
[ranking]            ← Caps tailoring to top-N by salary (KP_TAILORING_MAX_LISTINGS)
  │
  ▼
[tailoring]          ← Sonnet: generates tailored resume .docx per listing (parallel)
  │
  ▼
[cover_letter_tailoring] ← Sonnet: generates cover letter .docx per listing (parallel)
  │
  ▼
[tracking]           ← Upserts all listings + PipelineRun row into SQLite
  │
  ▼
[notification]       ← Logs summary (email stub, not yet wired)
  │
  ▼
 END
```

There is also a **manual graph** (`build_manual_graph`) for when you have a specific job URL and want to skip discovery entirely:

```
START → manual_extraction → job_analysis → tailoring → cover_letter_tailoring → tracking → notification → END
```

---

## 3. Node-by-Node Reference

### `discovery_node`
**What it does:** Runs all enabled provider scrapers concurrently, deduplicates results, applies a rule-based prefilter, and populates `state.discovered_listings` with `JobListing` objects (no description yet — just metadata from the search card).

**Providers used:** LinkedIn (browser), Indeed (browser), Greenhouse (HTTP API), Lever (HTTP API), Wellfound (browser), Workday (browser), direct-site configs (YAML-driven).

**Key interactions:**
- Reads `state.search_criteria` (keywords, target companies, salary floor, sources list)
- Calls `DiscoveryOrchestrator.run()` → each provider runs in a Playwright browser context or makes HTTP requests
- Calls `deduplicate_refs()` → checks `job_listings` table in SQLite; listings seen in prior runs are dropped
- Calls `apply_prefilter()` → rule-based keyword/role matching; controlled by `KP_DISCOVERY_PREFILTER_MIN_SCORE`
- Writes: `state.discovered_listings`

**What to watch in logs:**
```
discovery.complete  total_discovered=42  sources={linkedin: 30, greenhouse: 12}
dedup_complete      total_input=42  after_db=38  db_ok=true
```

---

### `filtering_node`
**What it does:** Applies the salary floor from `search_criteria.salary_floor`. Listings that fail are marked `FILTERED_OUT` (not removed — tracking still sees them).

**Logic:**
- `salary_min >= floor` → pass
- `salary_max >= floor` → pass (range straddles the floor)
- Both null → pass by default (`KP_FILTER_ALLOW_UNKNOWN_SALARY=true`)
- Else → filtered out

**Writes:** `state.qualified_listings`

**Route after:** If `qualified_listings` is empty → jumps directly to `notification`, skipping all LLM work.

---

### `bulk_extraction_node`
**What it does:** For each listing in `qualified_listings`, fetches the full job page and populates `listing.description`. Uses a layered extractor (HTTP first, browser fallback) with polite random delays (1.5–4s between requests).

**Why it's separate from discovery:** Discovery runs on search result *cards* (no page navigation, low detection risk). Bulk extraction navigates to each individual job URL — a heavier operation that only runs on the filtered-down set.

**Writes:** `listing.description`, `listing.title/company/location/salary_min/salary_max/remote` (fills any gaps from the card data)

**Failures:** Sets `listing.status = ERRORED`, appends to `state.errors`, continues with remaining listings.

---

### `job_analysis_node`
**What it does:** Sends each listing's description to **Claude Haiku** (`KP_JOB_ANALYSIS_MODEL`) and extracts a structured `JobAnalysisResult`:
- `themes` — what the employer actually cares about
- `seniority` — junior / mid / senior / lead / staff / director
- `domain_tags` — defense, ml, startup, energy, etc.
- `must_hit_keywords` — ATS-critical terms
- `priority_requirements`, `basic_qualifications`, `preferred_qualifications`
- `angles` — candidate positioning angles

**Parallelism:** Up to `KP_LLM_MAX_CONCURRENCY` (default 4) Haiku calls in flight simultaneously. Cache-key deduplication ensures two identical listings only trigger one call.

**In-run cache:** Results keyed by `dedup_key + sha256(description)[:16]`. Repeated runs that re-discover the same listing with the same JD text skip the LLM call entirely.

**Writes:** `state.job_analyses` (dict keyed by `dedup_key`)

---

### `ranking_node`
**What it does:** If `KP_TAILORING_MAX_LISTINGS > 0`, trims `qualified_listings` to the top-N by salary (salary_max desc, salary_min desc, unknowns last). Excess listings get `status = SKIPPED` so tracking records them.

Default is 0 (no cap — tailor everything). Set it to 3–5 during testing to limit spend.

---

### `tailoring_node`
**What it does:** For each listing in `qualified_listings`, sends the job analysis + candidate profile to **Claude Sonnet** and generates a structured `ResumeTailoringPlan`, then applies it deterministically to produce a `.docx` resume in `data/tailored_resumes/<run_id>/`.

**Two-phase approach:**
1. **Plan pass** (LLM): Sonnet reads the job analysis and candidate profile, outputs which bullets to include/rewrite and in what order.
2. **Apply pass** (deterministic): `apply_tailoring_plan()` assembles the docx from the plan — no second LLM call.

**Parallelism:** Same bounded concurrency as job_analysis.

**Caching:** No system-level caching here — each listing gets a unique plan since the profile is pruned to relevant tags per listing.

**Writes:** `listing.tailored_resume_path`, `listing.status = PENDING_REVIEW`

---

### `cover_letter_tailoring_node`
**What it does:** For each listing, generates a `CoverLetterPlan` via Sonnet and renders it to a `.docx` in `data/tailored_cover_letters/<run_id>/`.

**Prompt caching active here:** The static portion of the system prompt (cover letter objectives, style guide, tone rules, hard requirements) is sent with `cache_control: {type: "ephemeral"}`. On the second+ listing, Anthropic's servers skip re-encoding those ~1500 tokens — you pay 10% of their normal input cost instead of 100%.

See [Observing Prompt Caching](#7-observing-prompt-caching) below.

**Parallelism:** Same bounded concurrency.

**Writes:** `listing.tailored_cover_letter_path`, `listing.status = PENDING_REVIEW`

---

### `tracking_node`
**What it does:** Persists the run to SQLite. Two things:

1. **Listing upsert** — each listing in `qualified_listings` is written via `INSERT OR REPLACE` keyed on `dedup_key`. If the row already exists (re-discovered listing), mutable fields (status, paths, timestamps) are updated; immutable fields (created_at, id) stay.

2. **PipelineRun row** — a `pipeline_runs` record is written with counts and error summary. This is the audit trail.

**Why this matters for dedup:** The next run's `discovery_node` queries `job_listings` and excludes any `dedup_key` already present. Without tracking, you'd re-process every listing on every run.

**Database schema:**
```sql
job_listings (
    id INTEGER PRIMARY KEY,
    dedup_key TEXT UNIQUE NOT NULL,
    title, company, location, url, source, description,
    salary_min, salary_max, remote,
    status,
    tailored_resume_path, tailored_cover_letter_path,
    notes, created_at, updated_at
)

pipeline_runs (
    id INTEGER PRIMARY KEY,
    pipeline_name TEXT,
    status TEXT,  -- completed | failed
    started_at, completed_at,
    error_message TEXT,
    metadata_json TEXT  -- {run_id, discovered, qualified, tailored, ...}
)
```

---

### `notification_node`
**What it does:** Logs the final pipeline summary. Email sending is stubbed (marked TODO Milestone 4 — configure `KP_SMTP_*` settings when ready).

Sets `state.phase = COMPLETE`.

---

## 4. State Object

`JobAgentState` is the single object that flows through every node. Fields:

| Field | Type | Set by | Read by |
|---|---|---|---|
| `search_criteria` | `SearchCriteria` | `__main__` | discovery, filtering |
| `phase` | `PipelinePhase` | each node | logging, debugging |
| `discovered_listings` | `list[JobListing]` | discovery | filtering, tracking |
| `qualified_listings` | `list[JobListing]` | filtering | bulk_extraction, analysis, tailoring, tracking |
| `job_analyses` | `dict[str, JobAnalysisResult]` | job_analysis | tailoring, cover_letter |
| `job_analysis_cache` | `dict[str, JobAnalysisResult]` | job_analysis | job_analysis (cross-run cache) |
| `tailored_listings` | `list[JobListing]` | tailoring | tracking, notification |
| `errors` | `list[dict]` | any node | tracking, notification |
| `run_id` | `str` | `__main__` | all (log correlation) |
| `dry_run` | `bool` | `__main__` | discovery, bulk_extraction, tailoring, tracking |

**Note on LangGraph internals:** `graph.ainvoke()` returns a `dict`, not the dataclass. `coerce_state()` in `state.py` rehydrates it. This is why `__main__.py` does:
```python
raw_result = await graph.ainvoke(initial_state)
final_state = coerce_state(raw_result)
```

---

## 5. Database

**Location:** `data/platform.db` (SQLite, gitignored)

**Schema management:**
- `init_db()` — idempotent, called at startup, creates any missing tables
- Alembic — owns migrations: `alembic upgrade head`

**Inspect directly:**
```bash
sqlite3 data/platform.db

# List all listings
SELECT dedup_key, title, company, status, updated_at FROM job_listings ORDER BY updated_at DESC LIMIT 20;

# Check pipeline runs
SELECT pipeline_name, status, metadata_json FROM pipeline_runs ORDER BY id DESC LIMIT 5;

# Count by status
SELECT status, COUNT(*) FROM job_listings GROUP BY status;
```

**Reset between test runs:**
```bash
rm data/platform.db
python -c "import asyncio; from core.database import init_db; asyncio.run(init_db())"
```

---

## 6. Running the Pipeline

### Option A: Full pipeline (real browser + real LLM)

```bash
source .venv/bin/activate

# Human-readable logs, debug verbosity
KP_LOG_JSON=false KP_LOG_LEVEL=DEBUG python -m pipelines.job_agent
```

Limit tailoring spend during testing:
```bash
# Only tailor the top 2 listings by salary; analyse all
KP_TAILORING_MAX_LISTINGS=2 KP_LOG_JSON=false python -m pipelines.job_agent
```

### Option B: Discovery only (no LLM, no tailoring)

```bash
python scripts/run_discovery_node.py \
  --sources greenhouse,lever \
  --keywords "technical program manager" "senior engineer" \
  --companies anduril,anthropic,scale-ai
```

### Option C: Single job URL → full tailoring (no discovery)

```bash
python scripts/run_manual_url_tailor.py "https://jobs.lever.co/anduril/abc123"
```

This runs `manual_extraction → job_analysis → tailoring → cover_letter_tailoring → tracking`.

### Option D: Dry run (no network, no LLM, no DB writes)

```bash
python -m pipelines.job_agent --dry-run 2>/dev/null
# Completes in ~1s; validates the graph compiles and state flows correctly
```

*(Note: `--dry-run` flag not yet wired to `argparse` — set `initial_state.dry_run = True` directly in `__main__.py` for now.)*

---

## 7. Observing Prompt Caching

Prompt caching is active on the **cover letter tailoring** node. The static system prompt (instructions + style guide, ~1500 tokens) is sent with `cache_control: {type: "ephemeral"}`. On the second listing within the same run, Anthropic's API reuses the cached prefix.

### See it in logs

Run with human-readable logs:
```bash
KP_LOG_JSON=false python -m pipelines.job_agent
```

Look for `llm_request_complete` log lines. First listing:
```
llm_request_complete  cache_hit=false  cache_creation_tokens=312  cache_read_tokens=0  input_tokens=2100  cost_usd=0.000420
```

Second listing onward:
```
llm_request_complete  cache_hit=true   cache_creation_tokens=0    cache_read_tokens=312  input_tokens=1900  cost_usd=0.000095
```

The cost drop (~78% on the cached portion) is visible in the `cost_usd` field.

### How it's implemented

```
AnthropicClient.complete(cache_system=True)
  └── wraps system string as:
      [{type: "text", text: "...", cache_control: {type: "ephemeral"}}]
      └── Anthropic API caches tokens up to this breakpoint for 5 min

structured_complete(system_prefix=..., cache_system=True)
  └── prepends stable content (style guide) to the base JSON instruction
  └── forwards cache_system to client

TailoringEngine
  └── calls build_cached_system() once per run (not once per listing)
  └── passes result to every structured_complete call in the loop

cover_letter_tailoring_node
  └── build_cached_system = lambda _state, runtime: runtime.cached_system
  └── cached_system = style_guide + static instructions, rendered once in _prepare_runtime
```

**Cache validity:** 5-minute TTL on Anthropic's side. If your run takes longer than 5 minutes between the first and second listing, the cache will miss — check `cache_read_tokens=0` in logs.

### Why only cover letter (not resume tailoring)?

Resume tailoring prunes the candidate profile *per-listing* based on `domain_tags` — the system prompt content changes each call, so there's no stable prefix to cache. Cover letter tailoring uses a fixed style guide and fixed instructions that are identical across all listings in a run.

---

## 8. Testing

### Full automated suite (no credentials, mocked)

```bash
source .venv/bin/activate
python -m pytest -q                         # all 447 tests
python -m pytest core/tests/ -q             # core infrastructure
python -m pytest pipelines/job_agent/tests/ -q  # job agent nodes
python -m pytest pipelines/scraper/tests/ -q    # scraper pipeline
```

### Run a specific area

```bash
# Filtering logic
python -m pytest pipelines/job_agent/tests/test_filtering.py -v

# LinkedIn auth detection
python -m pytest pipelines/job_agent/tests/test_linkedin_provider.py -v

# Discovery deduplication (DB mock)
python -m pytest pipelines/job_agent/tests/test_discovery_deduplication.py -v

# Tailoring engine (parallelism + caching)
python -m pytest core/tests/test_workflows_tailoring.py core/tests/test_workflows_analysis.py -v

# Cover letter (includes rendering and validation)
python -m pytest pipelines/job_agent/tests/test_cover_letter_tailoring.py -v
```

### Verify the database after a real run

```bash
sqlite3 data/platform.db <<'SQL'
SELECT
  title, company, status,
  CASE WHEN tailored_resume_path IS NOT NULL THEN 'yes' ELSE 'no' END AS has_resume,
  CASE WHEN tailored_cover_letter_path IS NOT NULL THEN 'yes' ELSE 'no' END AS has_cover_letter
FROM job_listings
ORDER BY updated_at DESC
LIMIT 10;
SQL
```

### Verify parallelism is working

Watch timestamps in the log — job analysis calls for multiple listings should start within milliseconds of each other, not sequentially:

```bash
KP_LOG_JSON=false python -m pipelines.job_agent 2>&1 | grep "llm_request_start"
```

Sequential (old): timestamps 2–5s apart.
Parallel (new): timestamps 0–100ms apart, then a cluster of `llm_request_complete` as they resolve.

---

## 9. Key Settings Reference

All settings are prefixed `KP_` and can be set in `.env` or as environment variables.

| Setting | Default | Description |
|---|---|---|
| `KP_ANTHROPIC_API_KEY` | *(required)* | Anthropic API key |
| `KP_ANTHROPIC_MODEL` | `claude-sonnet-4-6` | Default model (tailoring, cover letter) |
| `KP_JOB_ANALYSIS_MODEL` | `claude-haiku-4-5-20251001` | Cheaper model for structured JD extraction |
| `KP_LLM_MAX_CONCURRENCY` | `4` | Max parallel LLM calls per engine pass |
| `KP_TAILORING_MAX_LISTINGS` | `0` | Cap on listings sent to tailoring (0 = no cap) |
| `KP_FILTER_ALLOW_UNKNOWN_SALARY` | `true` | Pass listings with no posted salary |
| `KP_DATABASE_URL` | `sqlite+aiosqlite:///data/platform.db` | DB connection string |
| `KP_DATABASE_ECHO` | `false` | Print raw SQL to stderr |
| `KP_LOG_JSON` | `true` | JSON logs (false = human readable) |
| `KP_LOG_LEVEL` | `INFO` | DEBUG / INFO / WARNING / ERROR |
| `KP_BROWSER_HEADLESS` | `true` | Run Playwright headless |
| `KP_BROWSER_RATE_LIMIT_SECONDS` | `5.0` | Min seconds between page navigations |
| `KP_DISCOVERY_MAX_CONCURRENT_PROVIDERS` | `2` | Parallel browser provider contexts |
| `KP_DISCOVERY_MAX_LISTINGS_PER_PROVIDER` | `150` | Hard cap per provider per run |
| `KP_LINKEDIN_EMAIL` / `KP_LINKEDIN_PASSWORD` | *(optional)* | LinkedIn auth credentials |
| `KP_GREENHOUSE_TARGET_COMPANIES` | *(optional)* | Comma-separated slugs e.g. `anduril,scale-ai` |
| `KP_LEVER_TARGET_COMPANIES` | *(optional)* | Comma-separated slugs e.g. `openai,anthropic` |
| `KP_RESUME_MASTER_PROFILE_PATH` | `pipelines/job_agent/context/candidate_profile.yaml` | Your resume profile |
| `KP_COVER_LETTER_STYLE_GUIDE_PATH` | `pipelines/job_agent/context/cover_letter_style.md` | Your writing style guide |
| `KP_RESUME_OUTPUT_DIR` | `data/tailored_resumes` | Where .docx resumes are saved |
| `KP_COVER_LETTER_OUTPUT_DIR` | `data/tailored_cover_letters` | Where .docx cover letters are saved |

---

*Generated from source — `core/`, `pipelines/job_agent/`, `core/workflows/`. Run `pytest -q` to verify the pipeline is healthy after any changes.*
