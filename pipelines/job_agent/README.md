# Job Application Agent

**Pipeline 1** on the Kokomoor Platform.

Automates job search, resume/cover letter tailoring, and application tracking.

## Pipeline Flow

```
Default: Discovery → Filtering → Job Analysis → Resume Tailoring → Cover-Letter Tailoring → Human Review → Application → Tracking → Notification
Manual:  Manual Extraction (URL) → Job Analysis → Resume Tailoring → Cover-Letter Tailoring → Tracking → Notification
```

### Nodes

| Node | Status | Description |
|------|--------|-------------|
| Discovery | Stub | Scrape job boards via Playwright |
| Manual Extraction (URL) | **Implemented** | Fetch direct job URL, extract and normalize a canonical `JobListing` |
| Filtering | Implemented | Salary floor, keyword, dedup filters |
| Job Analysis | **Implemented** | Full-JD structured extraction via LLM → `JobAnalysisResult` (themes, quals, keywords) |
| Tailoring | **Implemented** | Resume tailoring: plan + apply + render → `.docx` (consumes pre-computed analysis) |
| Cover-Letter Tailoring | **Implemented** | Cover-letter tailoring: structured plan + deterministic validation + `.docx` render |
| Human Review | Planned (M4) | Email notification, approval gate |
| Application | Planned (M4) | Playwright form-fill |
| Tracking | Stub | SQLite persistence |
| Notification | Stub | Email digest |

### Architecture (job analysis + tailoring)

The pipeline separates job understanding from resume tailoring:

1. **Job analysis node** (`nodes/job_analysis.py`) — reads the **full** `JobListing.description` (up to 30k chars, configurable), extracts structured signals via LLM → `JobAnalysisResult` (themes, seniority, domain tags, ATS keywords, basic/preferred qualifications, positioning angles). Cached by `dedup_key + description hash` to avoid stale reuse when JD text changes.
2. **Tailoring node** (`nodes/tailoring.py`) — consumes pre-computed `JobAnalysisResult` from state + tag-filtered master profile → `ResumeTailoringPlan` (1 LLM call per listing) → deterministic `apply_tailoring_plan` → `render_resume_docx`.

Output format matches the Kokomoor resume template (section borders, tab-aligned dates/locations, EDUCATION first). The master profile (`context/candidate_profile.yaml`, gitignored) uses schema v1: bullets have stable `id`, `tags`, `variants`; experience/education entries have `location` and optional `subtitle`. Copy `candidate_profile.example.yaml` to get started.

### Running

```bash
# From project root
python -m pipelines.job_agent
```

Manual truncated flow (single direct URL):

```bash
python scripts/run_manual_url_tailor.py "https://company.com/careers/job-123"
```

Writes under `data/tailored_resumes/<run-id>/` (or `KP_RESUME_OUTPUT_DIR` + run id):

- `*.docx` — tailored resume
- `extracted_job_<dedup_prefix>.md` — full scraped job description (untruncated, for verifying what the scraper captured)
- `job_analysis_<dedup_prefix>.md` — structured analysis output (what the tailoring node receives as context)
- `run-id` defaults to `manual-url-<timestamp>-<urlhash>` and can be overridden by passing a second CLI arg or `KP_MANUAL_RUN_ID`.

### Testing

```bash
pytest pipelines/job_agent/tests/ -v
```

### Configuration

All config via environment variables (prefix `KP_`). See `.env.example` at project root.

Key settings for this pipeline:
- `KP_FETCH_HTTP_TIMEOUT_SECONDS` / `KP_FETCH_HTTP_MAX_RETRIES` / `KP_FETCH_BROWSER_POST_WAIT_MS` — Shared HTTP/browser fetch (see `core.fetch`)
- `KP_FETCH_BROWSER_TIMEOUT_MS` — Browser navigation timeout in shared fetch transport
- `KP_ANTHROPIC_API_KEY` — Required for LLM nodes
- `KP_RESUME_MASTER_PROFILE_PATH` — Path to master resume profile YAML
- `KP_RESUME_OUTPUT_DIR` — Output directory for tailored resumes
- `KP_JOB_ANALYSIS_MODEL` — Model for job analysis node (default: `claude-haiku-4-5-20251001`)
- `KP_JOB_ANALYSIS_MAX_TOKENS` — Max output tokens for job analysis (default: `2048`)
- `KP_JOB_ANALYSIS_MAX_INPUT_CHARS` — Safety cap on JD length sent to LLM (default: `30000`)
- `KP_JOB_ANALYSIS_ENABLE_CACHE` — Cache analysis by `dedup_key + description hash` within a run (default: `true`)
- `KP_RESUME_PLAN_MODEL` — Model for tailoring plan pass (default: uses `KP_ANTHROPIC_MODEL`)
- `KP_RESUME_PLAN_MAX_TOKENS` — Max output tokens for plan pass (default: `2048`)
- `KP_RESUME_ENABLE_CRITIQUE` — Enable optional LLM critique pass (default: `false`)
- `KP_COVER_LETTER_MODEL` / `KP_COVER_LETTER_MAX_TOKENS` — Cover-letter plan call model/token limits
- `KP_COVER_LETTER_MAX_INPUT_CHARS` — Safety cap on JD length sent to cover-letter generation
- `KP_COVER_LETTER_STYLE_GUIDE_PATH` — Externalized style guide path
- `KP_BROWSER_HEADLESS` — Set `false` to watch browser automation

Manual extraction details:
- Fetches with the original URL to preserve query parameters; canonicalizes the resolved final URL for provider/source detection and dedup.
- Description selection is quality-based across structured/provider/generic/fallback candidates (not fixed source precedence).
- `JobListing.description` stores cleaned canonical text; raw extracted text is preserved in extraction notes for debugging.
- Status transitions: `DISCOVERED → ANALYZING → ANALYZED → TAILORING → PENDING_REVIEW` (or `ERRORED` at any failure point).
