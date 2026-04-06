# Job Application Agent

**Pipeline 1** on the Kokomoor Platform.

Automates job search, resume/cover letter tailoring, and application tracking.

## Pipeline Flow

```
Discovery → Filtering → Tailoring → Human Review → Application → Tracking → Notification
```

### Nodes

| Node | Status | Description |
|------|--------|-------------|
| Discovery | Stub | Scrape job boards via Playwright |
| Filtering | Implemented | Salary floor, keyword, dedup filters |
| Tailoring | **Implemented** | Multi-phase LLM resume tailoring → `.docx` |
| Human Review | Planned (M4) | Email notification, approval gate |
| Application | Planned (M4) | Playwright form-fill |
| Tracking | Stub | SQLite persistence |
| Notification | Stub | Email digest |

### Tailoring architecture

The tailoring node runs two LLM passes per listing via `structured_complete`, then assembles and renders deterministically:

1. **Job analysis** — extract themes, seniority, domain tags from the JD → `JobAnalysisResult`
2. **Tailoring plan** — select/order/rewrite master-profile bullets → `ResumeTailoringPlan`
3. **Apply plan** — deterministic assembly → `TailoredResumeDocument`
4. **Render .docx** — styled Word document written to `data/tailored_resumes/{run_id}/`

The master profile (`context/candidate_profile.yaml`, gitignored) uses schema v1: each bullet has a stable `id`, `tags`, and optional `variants`. Copy `candidate_profile.example.yaml` to get started.

### Running

```bash
# From project root
python -m pipelines.job_agent
```

### Testing

```bash
pytest pipelines/job_agent/tests/ -v
```

### Configuration

All config via environment variables (prefix `KP_`). See `.env.example` at project root.

Key settings for this pipeline:
- `KP_ANTHROPIC_API_KEY` — Required for Tailoring node
- `KP_RESUME_MASTER_PROFILE_PATH` — Path to master resume profile YAML (default: `pipelines/job_agent/context/candidate_profile.yaml`)
- `KP_RESUME_OUTPUT_DIR` — Output directory for tailored resumes (default: `data/tailored_resumes`)
- `KP_RESUME_ENABLE_CRITIQUE` — Enable optional LLM critique pass (default: `false`)
- `KP_BROWSER_HEADLESS` — Set `false` to watch browser automation
- `KP_BROWSER_RATE_LIMIT_SECONDS` — Minimum delay between page loads (default: 5s)
