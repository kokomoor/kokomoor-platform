# AGENTS.md — pipelines/job_agent/

Job search automation pipeline. Discovers listings, filters by criteria, tailors materials, and tracks applications.

## Pipeline flow

```
Discovery → Filtering → Tailoring → Human Review → Application → Tracking → Notification
```

## Current status

| Node | Status | Notes |
|------|--------|-------|
| Discovery | **Stub** | Returns empty list. M2: real scraping via `BrowserManager` + `structured_complete` |
| Filtering | **Implemented** | Salary floor filter. Keyword/role filters planned. |
| Tailoring | **Planned (M3)** | Claude-generated resume + cover letter |
| Human Review | **Planned (M4)** | Email notification, approval gate |
| Application | **Planned (M4)** | Playwright form-fill |
| Tracking | **Stub** | Logs only. M2: upsert via `core.database` |
| Notification | **Stub** | Logs only. M4: email via `core.notifications` |

## File map

| File | Role |
|------|------|
| `__main__.py` | Entry point: `python -m pipelines.job_agent` |
| `graph.py` | LangGraph `StateGraph` wiring — nodes, edges, conditional routing |
| `state.py` | `JobAgentState` dataclass + `PipelinePhase` enum |
| `models/` | `JobListing` (SQLModel, persisted), `SearchCriteria` / `JobFilter` (Pydantic, transient) |
| `nodes/` | One file per node — pure `async (state) -> state` functions |
| `prompts/` | Markdown templates with `{placeholder}` format strings |
| `context/candidate_profile.yaml` | Structured candidate data consumed by the Tailoring node |
| `tools/` | LLM tool definitions (currently empty) |
| `tests/` | Unit tests with `MockLLMClient` and no real API calls |

## Key rules

- **Nodes are pure functions:** `async def node(state: JobAgentState) -> JobAgentState`. No side effects beyond logging and DB writes.
- **State is centralized:** add new pipeline data to `JobAgentState` in `state.py`, not to individual nodes.
- **Models are split by persistence:** `JobListing` is a DB table (`SQLModel, table=True`). `SearchCriteria` and `JobFilter` are transient Pydantic models. Don't mix these patterns.
- **Never auto-submit.** The pipeline must pause for human approval before application submission. This is a hard product constraint.
- **Anti-detection is mandatory.** All browser interactions go through `core.browser.BrowserManager`. Use `rate_limited_goto()` and `human_delay()`. Never raw Playwright.
- **Prefer APIs/aggregators** over scraping where available. Discovery should check RSS feeds or public APIs before falling back to browser scraping.

## Prompt templates

Templates in `prompts/` use `{placeholder}` syntax:
- `{candidate_profile}` — from `context/candidate_profile.yaml`
- `{job_title}`, `{company}`, `{job_description}` — from `JobListing`
- `{output_schema}` — Pydantic model JSON schema

The cover letter prompt defines a **quality benchmark**: direct, concrete, no filler, 250-400 words. Maintain this standard.

## Context folder (`context/`)

Contains reference materials consumed by the Tailoring node. **Not version-controlled** (`context/` is gitignored).

| File(s) | Purpose |
|---------|---------|
| `candidate_profile.yaml` | Structured profile data fed to prompts. **Primary input** for tailoring. |
| `Resume_Kokomoor_*.pdf/.docx` | Multiple resume versions for different positioning (defense-lead, tech-lead, startup-lead). Quality and voice benchmarks for generated resumes. |
| `Anduril.docx`, `CoverLetter_*.docx` | Cover letter examples. The Anduril letter is the explicit quality target referenced in `prompts/tailor_cover_letter.md`. |
| `Gauntlet-42_*.docx`, `Gauntlet - Dealum.pdf`, `Gauntlet_Pitch_Deck_*.pdf`, `Spyglass.pdf` | Startup/product depth. Use when tailoring for AI, startup, or PropTech roles — Spyglass is an "AI-native automated public-records researcher" with OCR/NLP/LLM extraction pipelines. |
| `master_context.md` | Full candidate constitution — profile, job search params, architecture rationale, writing voice, milestone plan. |
| `progress_log.md` | Rolling session state for LLM continuity across development sessions. |
| Academic records (transcripts, grade reports, MIT application) | Source material for `candidate_profile.yaml`. Not directly consumed by code. |

## Testing

```bash
pytest pipelines/job_agent/tests/ -v
```

- Use `MockLLMClient` for LLM calls.
- Use `get_test_session()` for database tests.
- No real API calls, no real browser sessions.
- Import `SearchCriteria` from `pipelines.job_agent.models`, not from `state`.

## Common mistakes

- Importing `SearchCriteria` from `state` instead of `models` — mypy will reject it (no implicit re-export).
- Hardcoding salary floor in node logic — use `state.search_criteria.salary_floor`.
- Adding graph wiring before the node exists — `graph.py` has commented TODOs for future nodes.
- Forgetting to cast `graph.ainvoke()` result — LangGraph types it loosely; use `cast("JobAgentState", ...)`.
