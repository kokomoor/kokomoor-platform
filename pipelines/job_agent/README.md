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
| Tailoring | Planned (M3) | Claude-generated resume + cover letter |
| Human Review | Planned (M4) | Email notification, approval gate |
| Application | Planned (M4) | Playwright form-fill |
| Tracking | Stub | SQLite persistence |
| Notification | Stub | Email digest |

### Running

```bash
# From project root
python -m pipelines.job_agent
```

### Testing

```bash
pytest pipelines/job_agent/tests/ -v
```

## Configuration

All config via environment variables (prefix `KP_`). See `.env.example` at project root.

Key settings for this pipeline:
- `KP_ANTHROPIC_API_KEY` — Required for Tailoring node
- `KP_BROWSER_HEADLESS` — Set `false` to watch browser automation
- `KP_BROWSER_RATE_LIMIT_SECONDS` — Minimum delay between page loads (default: 5s)
