# Job Application Agent

**Pipeline 1** on the Kokomoor Platform.

Automates job search, resume/cover letter tailoring, and application tracking.

## Pipeline Flow

```
Default: Discovery → Filtering → Tailoring → Human Review → Application → Tracking → Notification
Manual:  Manual Extraction (URL) → Tailoring → Tracking → Notification
```

### Nodes

| Node | Status | Description |
|------|--------|-------------|
| Discovery | Stub | Scrape job boards via Playwright |
| Manual Extraction (URL) | **Implemented** | Fetch direct job URL, extract and normalize a canonical `JobListing` |
| Filtering | Implemented | Salary floor, keyword, dedup filters |
| Tailoring | **Implemented** | Multi-phase LLM resume tailoring → `.docx` |
| Human Review | Planned (M4) | Email notification, approval gate |
| Application | Planned (M4) | Playwright form-fill |
| Tracking | Stub | SQLite persistence |
| Notification | Stub | Email digest |

### Tailoring architecture

The tailoring node runs two LLM passes per listing via `structured_complete`, then assembles and renders deterministically:

1. **Job analysis** — extract themes, seniority, domain tags from the JD → `JobAnalysisResult` (cheap model)
2. **Tailoring plan** — select/order/rewrite master-profile bullets → `ResumeTailoringPlan` (full model, tag-filtered profile)
3. **Apply plan** — deterministic assembly → `TailoredResumeDocument`
4. **Render .docx** — Times New Roman template-format resume to `data/tailored_resumes/{run_id}/`

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

### Testing

```bash
pytest pipelines/job_agent/tests/ -v
```

### Configuration

All config via environment variables (prefix `KP_`). See `.env.example` at project root.

Key settings for this pipeline:
- `KP_FETCH_HTTP_TIMEOUT_SECONDS` / `KP_FETCH_HTTP_MAX_RETRIES` / `KP_FETCH_BROWSER_POST_WAIT_MS` — Shared HTTP/browser fetch (see `core.fetch`)
- `KP_ANTHROPIC_API_KEY` — Required for Tailoring node
- `KP_RESUME_MASTER_PROFILE_PATH` — Path to master resume profile YAML
- `KP_RESUME_OUTPUT_DIR` — Output directory for tailored resumes
- `KP_RESUME_ANALYSIS_MODEL` — Cheap model for job analysis pass (default: `claude-haiku-4-5-20251001`)
- `KP_RESUME_PLAN_MODEL` — Model for tailoring plan pass (default: uses `KP_ANTHROPIC_MODEL`)
- `KP_RESUME_ANALYSIS_MAX_TOKENS` / `KP_RESUME_PLAN_MAX_TOKENS` — Per-phase output caps
- `KP_RESUME_ENABLE_ANALYSIS_CACHE` — Cache analysis by dedup_key within a run (default: `true`)
- `KP_RESUME_ENABLE_CRITIQUE` — Enable optional LLM critique pass (default: `false`)
- `KP_BROWSER_HEADLESS` — Set `false` to watch browser automation
