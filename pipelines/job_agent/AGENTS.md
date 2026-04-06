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
| Tailoring | **Implemented** | Multi-phase LLM resume tailoring → `.docx`. See below. |
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
| `resume/` | Tailoring subsystem: profile loading, plan application, `.docx` rendering |
| `prompts/` | Markdown templates with `{placeholder}` format strings |
| `context/candidate_profile.yaml` | Structured candidate data consumed by the Tailoring node (gitignored) |
| `tools/` | LLM tool definitions (currently empty) |
| `tests/` | Unit tests with `MockLLMClient` and no real API calls |

## Key rules

- **Nodes are pure functions:** `async def node(state: JobAgentState) -> JobAgentState`. No side effects beyond logging and DB writes.
- **State is centralized:** add new pipeline data to `JobAgentState` in `state.py`, not to individual nodes.
- **Models are split by persistence:** `JobListing` is a DB table (`SQLModel, table=True`). `SearchCriteria` and `JobFilter` are transient Pydantic models. Don't mix these patterns.
- **Never auto-submit.** The pipeline must pause for human approval before application submission. This is a hard product constraint.
- **Anti-detection is mandatory.** All browser interactions go through `core.browser.BrowserManager`. Use `rate_limited_goto()` and `human_delay()`. Never raw Playwright.
- **Prefer APIs/aggregators** over scraping where available. Discovery should check RSS feeds or public APIs before falling back to browser scraping.

## Resume tailoring architecture

The tailoring node runs **inside a single LangGraph node** with multiple internal phases:

1. **Job analysis** (`tailor_job_analysis.md`) — extract themes, seniority, domain tags from the JD. Output: `JobAnalysisResult`.
2. **Tailoring plan** (`tailor_resume_plan.md`) — select/order/rewrite bullets using master-profile IDs. Output: `ResumeTailoringPlan`.
3. **Apply plan** (`resume/applier.py`) — deterministic assembly: master profile + plan → `TailoredResumeDocument`.
4. **Render .docx** (`resume/renderer.py`) — `TailoredResumeDocument` → styled Word document.

Convention: **mutate listings in place** — set `tailored_resume_path` and `status` on each `JobListing`. After the node, `state.tailored_listings` aliases `state.qualified_listings` (same list, same object references).

The master profile YAML uses **schema v1**: each bullet has a stable `id`, `tags` list, and optional `variants` dict (`short`/`long`). The LLM references IDs; the applier resolves them deterministically.

### Cost optimizations (all configurable)

| Feature | Config flag | Default | Effect |
|---------|------------|---------|--------|
| Model split | `KP_RESUME_ANALYSIS_MODEL` | `claude-haiku-4-5-20251001` | Analysis pass uses cheaper model; plan pass uses `KP_ANTHROPIC_MODEL` |
| Plan model override | `KP_RESUME_PLAN_MODEL` | `""` (= default) | Override the plan-pass model separately |
| Analysis max tokens | `KP_RESUME_ANALYSIS_MAX_TOKENS` | `1024` | Caps output length for analysis JSON |
| Plan max tokens | `KP_RESUME_PLAN_MAX_TOKENS` | `2048` | Caps output length for plan JSON |
| Analysis cache | `KP_RESUME_ENABLE_ANALYSIS_CACHE` | `true` | Reuses analysis results for duplicate `dedup_key` within a run |
| Context pruning | automatic | always on | Only profile bullets whose tags match the analysis domain tags are sent to the plan pass |

Context pruning: the `format_profile_for_llm` function accepts a `relevant_tags` set. The node expands the analysis `domain_tags` via `_TAG_EXPANSION` (synonym mapping) and adds universally-relevant tags (`leadership`, `technical`, etc.) so important bullets are never dropped. Sections with no matching bullets are omitted entirely, reducing prompt tokens.

### Resume models (`models/resume_tailoring.py`)

| Model | Purpose |
|-------|---------|
| `ResumeMasterProfile` | Loaded from YAML — all possible bullets with IDs, tags, locations, subtitles |
| `JobAnalysisResult` | LLM pass 1 output — themes, seniority, domain tags |
| `ResumeTailoringPlan` | LLM pass 2 output — bullet selection, ordering, ops |
| `TailoredResumeDocument` | Post-application structure with location/subtitle/additional_info, input to `.docx` renderer |

### Resume package (`resume/`)

| File | Role |
|------|------|
| `profile.py` | Load YAML → `ResumeMasterProfile`; format profile text for LLM (with optional tag filtering) |
| `applier.py` | Pure function: `(profile, plan) → TailoredResumeDocument` with location/subtitle passthrough |
| `renderer.py` | `TailoredResumeDocument → .docx` matching Kokomoor template (Times New Roman, section borders, tab-aligned dates) |

### Renderer format spec

The renderer produces `.docx` files matching the Kokomoor resume template (Feb/Sep/Mar reference documents):
- **Font**: Times New Roman 11.5pt throughout, ALL CAPS via `w:caps` flag on section headers and company/school names
- **Layout**: US Letter, 0.5" top/bottom margins, 0.65" left/right margins, 10pt minimum line spacing
- **Section order**: EDUCATION → EXPERIENCE → TECHNICAL SKILLS → ADDITIONAL INFORMATION
- **Section headers**: Bold, ALL CAPS, black 1.5pt bottom border
- **Company/School**: Bold, ALL CAPS, right-tab-aligned location at 7.19"
- **Subtitle**: Optional italic line (e.g., "Defense Contractor", "PropTech B2B SaaS Startup")
- **Title/Degree**: Bold+italic (title) or italic (degree), right-tab-aligned dates
- **Bullets**: Indented list with left=270 hanging=280 twips
- **Line spacing**: 10pt AT_LEAST (allows text to breathe with 11.5pt font)
- **Spacers**: Non-breaking-space paragraph between each experience entry; no spacer between education entries
- **Experience**: Most recent first (ordering determined by LLM plan)
- **Page-fill**: Resume must fill at least one full page; never leave whitespace at the bottom. Slightly over one page is acceptable for senior roles. This is enforced via prompt guidance in `tailor_resume_plan.md`.

## Prompt templates

Templates in `prompts/` use `{placeholder}` syntax:
- `tailor_job_analysis.md` — `{job_title}`, `{company}`, `{job_description}`
- `tailor_resume_plan.md` — `{job_analysis}`, `{candidate_profile_structured}`, `{positioning_rules}`
- `tailor_cover_letter.md` — `{candidate_profile}`, `{job_title}`, `{company}`, `{job_description}`, `{output_schema}`

The cover letter prompt defines a **quality benchmark**: direct, concrete, no filler, 250-400 words. Maintain this standard.

## Context folder (`pipelines/job_agent/context/`)

Reference materials for the Tailoring node. **Private files here are gitignored** (see root `.gitignore`); only `candidate_profile.example.yaml` ships in git — copy it to `candidate_profile.yaml` locally and edit. Repo-root `/context/` (gitignored) can hold bulk assets (PDFs, decks) if you prefer not to keep them under the pipeline path.

| File(s) | Purpose |
|---------|---------|
| `candidate_profile.yaml` | Structured profile data fed to prompts. **Primary input** for tailoring. Local only. |
| `candidate_profile.example.yaml` | Committed template; safe to push. |
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
- Forgetting to cast `graph.ainvoke()` result — LangGraph types it loosely; use `cast("JobAgentState", ...)`.
- Referencing bullet IDs in prompts or tests that don't exist in the master profile — validate against `ResumeMasterProfile.all_bullet_ids()`.
- Using `state.tailored_listings` without checking `tailored_resume_path` — some listings may have failed tailoring; check per-listing path before downstream processing.
- Passing `llm_client` to `build_graph()` without `MockLLMClient` responses matching the expected call count — tailoring makes 2 calls per listing (analysis + plan), but with `KP_RESUME_ENABLE_ANALYSIS_CACHE=true` duplicate `dedup_key` listings share a single analysis call.
- Setting `KP_RESUME_ANALYSIS_MODEL` to empty string means the analysis falls back to the default `KP_ANTHROPIC_MODEL` (Sonnet), which is more expensive. Only do this if Haiku quality is insufficient for a specific use case.
