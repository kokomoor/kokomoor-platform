# AGENTS.md — pipelines/job_agent/

Job search automation pipeline. Discovers listings, filters by criteria, analyses JDs, tailors materials, and tracks applications.

## Pipeline flow

```
Default: Discovery → Filtering → Bulk Extraction → Job Analysis → Tailoring → Cover Letter Tailoring → Human Review → Application → Tracking → Notification
Manual:  Manual Extraction (URL) → Job Analysis → Tailoring → Cover Letter Tailoring → Tracking → Notification
```

## Current status

| Node | Status | Notes |
|------|--------|-------|
| Discovery | **Implemented** | Orchestrates browser + HTTP providers, deduplicates, prefilters, emits `JobListing` (no description). |
| Manual Extraction (URL) | **Implemented** | Fetches a single direct job URL and emits one canonical `JobListing` into `qualified_listings` |
| Filtering | **Implemented** | Salary floor filter. Keyword/role filters planned. |
| Bulk Extraction | **Implemented** | Fetches full job page content for each qualified listing after filtering. Populates `description`. |
| Job Analysis | **Implemented** | Dedicated LLM node: full JD → `JobAnalysisResult` (themes, quals, keywords). Stored on `state.job_analyses`. |
| Tailoring | **Implemented** | Plan pass only (1 LLM call), consumes pre-computed analysis from state. Render → `.docx`. |
| Cover Letter Tailoring | **Implemented** | 1 structured LLM call per listing. Validated against banned phrases, prose grounding, and evidence mapping. Render → `.docx`. |
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
| `models/resume_tailoring.py` | `JobAnalysisResult`, `ResumeTailoringPlan`, master profile types — shared contract between job-analysis and tailoring nodes |
| `nodes/` | One file per node — pure `async (state) -> state` functions |
| `nodes/bulk_extraction.py` | Bulk extraction node: fetches full job descriptions for qualified listings |
| `nodes/job_analysis.py` | Job analysis node: full JD → structured `JobAnalysisResult` via LLM |
| `nodes/cover_letter_tailoring.py` | Cover letter tailoring node: 1 LLM call per listing → validated `.docx` |
| `extraction/` | Job-specific URL → `JobListing` (layered parsing); **transport** via `core.fetch`; `inspection.py` writes markdown artifacts for manual review |
| `resume/` | Resume tailoring subsystem: profile loading, plan application, `.docx` rendering |
| `cover_letter/` | Cover letter subsystem: models, prompting, validation, applier, `.docx` rendering |
| `utils.py` | Shared utilities: tag expansion, positioning rules, safe filenames |
| `prompts/` | Markdown templates with `{placeholder}` format strings |
| `context/candidate_profile.yaml` | Structured candidate data consumed by the Tailoring node (gitignored). Includes `cover_letter` section for voice/style preferences. |
| `context/cover_letter_style.md` | Cover letter style guide with voice benchmarks from reference letters |
| `tools/` | LLM tool definitions (currently empty) |
| `tests/` | Unit tests with `MockLLMClient` and no real API calls |

## Discovery subsystem

The `pipelines/job_agent/discovery/` package implements browser-based and HTTP-based job listing collection. It is self-contained: all discovery-specific models, session management, rate limiting, and provider adapters live here.

**Internal data type:** `ListingRef` (not `JobListing`) flows within the discovery subsystem. It is a lightweight frozen dataclass carrying only what's visible on a search result card. Conversion to the persisted `JobListing` happens via `ref_to_job_listing()` at the end of the discovery node, after deduplication and prefiltering.

**Session persistence:** browser sessions (cookies, localStorage) are saved to `data/sessions/` (gitignored) by `SessionStore`. Sessions survive between runs and are restored automatically on the next run to maintain established logins and avoid triggering bot detection.

**Configuration:** `DiscoveryConfig.from_settings(get_settings())` is the canonical way to build the discovery config in the node. It maps `KP_DISCOVERY_*` env vars into the subsystem's typed config object.

**Provider tiers:**
- **Browser providers** (LinkedIn, Indeed, Built In, Wellfound, Workday) — use `BrowserManager` for Playwright-based scraping with stealth, rate limiting, and session persistence.
- **HTTP API providers** (Greenhouse, Lever) — use `core.fetch.HttpFetcher.fetch_json()`. No `BrowserManager` needed since these are public JSON APIs.

See `discovery/AGENTS.md` for the full subsystem contract, data flow, and provider implementation checklist.

## Key rules

- **Nodes are pure functions:** `async def node(state: JobAgentState) -> JobAgentState`. No side effects beyond logging and DB writes.
- **State is centralized:** add new pipeline data to `JobAgentState` in `state.py`, not to individual nodes.
- **Manual URL runs:** set `state.manual_job_url` and use `build_manual_graph()` for direct URL workflows.
- **Dry run semantics:** dry runs avoid network and LLM side effects. Manual extraction intentionally returns no listings in dry run mode.
- **Models are split by persistence:** `JobListing` is a DB table (`SQLModel, table=True`). `SearchCriteria` and `JobFilter` are transient Pydantic models. Don't mix these patterns.
- **Schema ownership:** `JobAnalysisResult` lives in `models/resume_tailoring.py` — the shared contract between the job-analysis node (producer) and the tailoring node (consumer). Both import from the same module. If a future node needs the same analysis, it imports from the same place.
- **Never auto-submit.** The pipeline must pause for human approval before application submission. This is a hard product constraint.
- **Anti-detection is mandatory.** All browser interactions go through `core.browser.BrowserManager`. Use `rate_limited_goto()` and `human_delay()`. Never raw Playwright.
- **Prefer APIs/aggregators** over scraping where available. Discovery should check RSS feeds or public APIs before falling back to browser scraping.
- **Extractor contract:** fetch with original URL, canonicalize the resolved final URL for provider/source detection and dedup, score description candidates by quality (structured/provider/generic/fallback), and keep `JobListing.description` as cleaned canonical text. Raw extracted text is preserved in notes for debugging.
- **Status transitions:** `DISCOVERED → ANALYZING → ANALYZED → TAILORING → COVER_LETTER_TAILORING → PENDING_REVIEW` (or `ERRORED` at any failure point). Analysis/tailoring failures set `ApplicationStatus.ERRORED`; avoid logic that assumes all qualified listings become tailored.

## Job analysis node

Dedicated LangGraph node (`nodes/job_analysis.py`) that sits between extraction/filtering and tailoring:

- **Input:** full `JobListing.description` (up to `KP_JOB_ANALYSIS_MAX_INPUT_CHARS`, default 30k)
- **Output:** `JobAnalysisResult` stored on `state.job_analyses[dedup_key]`
- **Prompt:** `prompts/tailor_job_analysis.md`
- **Caching:** by `dedup_key + description hash` within a run (configurable via `KP_JOB_ANALYSIS_ENABLE_CACHE`)

### Why a separate node?

The scraper captures the full job page content (qualifications, requirements, description — everything). Previously, the analysis was embedded inside the tailoring node and truncated to 4k chars, silently dropping qualifications sections. Separating it:
1. Ensures the **full JD** is analysed (no silent truncation)
2. Makes analysis results reusable by future nodes (cover letter, scoring)
3. Allows independent model/token configuration
4. Makes the graph debuggable — you can inspect `state.job_analyses` between nodes

## Tailoring node

Consumes pre-computed `JobAnalysisResult` from `state.job_analyses`. Runs one LLM call per listing (plan pass only).

1. **Tailoring plan** (`tailor_resume_plan.md`) — select/order/rewrite bullets using master-profile IDs. Output: `ResumeTailoringPlan`.
2. **Apply plan** (`resume/applier.py`) — deterministic assembly: master profile + plan → `TailoredResumeDocument`.
3. **Render .docx** (`resume/renderer.py`) — `TailoredResumeDocument` → styled Word document.

Convention: **mutate listings in place** — set `tailored_resume_path` and `status` on each `JobListing`. After the node, `state.tailored_listings` aliases `state.qualified_listings` (same list, same object references).

The master profile YAML uses **schema v1**: each bullet has a stable `id`, `tags` list, and optional `variants` dict (`short`/`long`). The LLM references IDs; the applier resolves them deterministically.

### Cost optimizations (all configurable)

| Feature | Config flag | Default | Effect |
|---------|------------|---------|--------|
| Analysis model | `KP_JOB_ANALYSIS_MODEL` | `claude-haiku-4-5-20251001` | Cheaper model for structured JD extraction |
| Analysis max tokens | `KP_JOB_ANALYSIS_MAX_TOKENS` | `2048` | Caps output length for analysis JSON |
| Analysis input cap | `KP_JOB_ANALYSIS_MAX_INPUT_CHARS` | `30000` | Safety cap on JD length sent to LLM |
| Analysis cache | `KP_JOB_ANALYSIS_ENABLE_CACHE` | `true` | Reuses analysis for duplicate `dedup_key` within a run |
| Plan model override | `KP_RESUME_PLAN_MODEL` | `""` (= default) | Override the plan-pass model separately |
| Plan max tokens | `KP_RESUME_PLAN_MAX_TOKENS` | `2048` | Caps output length for plan JSON |
| Context pruning | automatic | always on | Only profile bullets whose tags match the analysis domain tags are sent to the plan pass |

Context pruning: the profile formatter functions accept a `relevant_tags` set. Both tailoring nodes expand the analysis `domain_tags` via `expand_domain_tags()` in `utils.py` (synonym mapping + universally-relevant tags like `leadership`, `technical`) so important bullets are never dropped. Sections with no matching bullets are omitted entirely, reducing prompt tokens.

### Resume models (`models/resume_tailoring.py`)

| Model | Purpose |
|-------|---------|
| `ResumeMasterProfile` | Loaded from YAML — all possible bullets with IDs, tags, locations, subtitles |
| `CoverLetterPreferences` | Optional profile knobs for cover-letter voice, banned phrases, positioning angles, narrative themes |
| `JobAnalysisResult` | Job-analysis node output — themes, seniority, domain tags, basic/preferred qualifications |
| `ResumeTailoringPlan` | Tailoring plan pass output — bullet selection, ordering, ops |
| `TailoredResumeDocument` | Post-application structure with location/subtitle/additional_info, input to `.docx` renderer |

### Resume package (`resume/`)

| File | Role |
|------|------|
| `profile.py` | Load YAML → `ResumeMasterProfile`; format profile text for LLM (with optional tag filtering) |
| `applier.py` | Pure function: `(profile, plan) → TailoredResumeDocument` with location/subtitle passthrough |
| `renderer.py` | `TailoredResumeDocument → .docx` matching Kokomoor template (Times New Roman, section borders, tab-aligned dates) |

## Cover letter tailoring node

Runs after resume tailoring. Consumes the same `JobAnalysisResult` from `state.job_analyses`. One structured LLM call per listing producing a `CoverLetterPlan`, which is validated, applied, and rendered to `.docx`.

### Cover letter package (`cover_letter/`)

| File | Role |
|------|------|
| `models.py` | `CoverLetterPlan` (LLM output), `CoverLetterDocument` (renderer input), `RequirementEvidence`, `ToneVersion` literal |
| `prompting.py` | `build_cover_letter_prompt()` — assembles template + style guide + inventory + analysis |
| `profile.py` | `format_cover_letter_inventory()` — profile evidence with IDs for cover letters; `load_cover_letter_style_guide()` |
| `validation.py` | Normalization + two-tier validation (hard failures + soft warnings) |
| `applier.py` | `apply_cover_letter_plan()` — plan → document passthrough |
| `renderer.py` | `render_cover_letter_docx()` — business-letter `.docx` (Times New Roman 11pt, standard margins) |

### Validation architecture

Two categories of checks, run in order after normalization:

**Hard checks (ValueError → listing ERRORs):**
- ID references exist in profile
- Evidence mapping consistency (requirement_evidence IDs ⊆ selected_bullet_ids)
- No placeholders (`[Company]`, `{{var}}`, `TBD`)
- Complete sentences (terminal punctuation)
- Minimum evidence (≥2 bullet IDs, ≥1 experience ID)
- No banned phrases — two-tier system:
  - **Core banned phrases** (31 entries): always rejected regardless of profile. Covers AI-tell filler ("proven track record", "I am passionate about", etc.)
  - **Profile banned phrases** (`CoverLetterPreferences.banned_phrases`): candidate-specific voice constraints, also hard failures
- No generic openers ("I am writing to express my interest", etc.)
- Company motivation substance (≥10 words of specific reasoning)
- Prose grounding: for each `requirement_evidence` entry, at least one substantive token (numbers, 5+ char non-stopwords) from the cited bullets must appear in the body text

**Soft checks (warnings, logged but not blocking):**
- Paragraph duplicate claims (Jaccard similarity > 0.65)
- Company name missing from `company_motivation` or body
- Word budget (280–420 words)
- AI-tell word density (≥3 of: delve, tapestry, synergy, paradigm, etc.)
- Company motivation substance not reflected in body text

### Tone version

`tone_version` is a `Literal["confident_direct", "professional_narrative", "technical_precise"]`. The prompt explains each option; the LLM must choose one. This replaces the previous unconstrained string field.

### Quality mechanisms

Quality is enforced through layered defenses:
1. **Style guide** (`context/cover_letter_style.md`) — includes voice benchmark excerpts from real candidate letters
2. **Prompt constraints** — 10 hard requirements including no generic openers, no repeated claims, specific phrases auto-rejected
3. **Profile preferences** (`candidate_profile.yaml` → `cover_letter` section) — banned phrases, positioning angles, narrative themes, emphasis controls
4. **Structural validation** — prose grounding, evidence mapping, company motivation substance
5. **Positioning rules** — domain-specific one-liners from `utils.positioning_rules()` based on `domain_tags`

### Cost optimizations

| Feature | Config flag | Default | Effect |
|---------|------------|---------|--------|
| Cover letter model | `KP_COVER_LETTER_MODEL` | `""` (= default) | Override the cover letter model |
| Cover letter max tokens | `KP_COVER_LETTER_MAX_TOKENS` | `2200` | Caps output length |
| Cover letter input cap | `KP_COVER_LETTER_MAX_INPUT_CHARS` | `12000` | Safety cap on JD chars sent |
| Style guide path | `KP_COVER_LETTER_STYLE_GUIDE_PATH` | `context/cover_letter_style.md` | External style guide with voice benchmarks |

## Resume renderer format spec

The resume renderer produces `.docx` files matching the Kokomoor resume template (Feb/Sep/Mar reference documents):
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

## Inspection artifacts

The manual run script (`scripts/run_manual_url_tailor.py`) writes two markdown files alongside the `.docx`:

- `extracted_job_<prefix>.md` — full canonical scraped description (untruncated, for verifying scraper output)
- `job_analysis_<prefix>.md` — structured analysis (themes, quals, keywords — what the tailoring node uses)

These are produced by `extraction/inspection.py`.
Run IDs default to `manual-url-<timestamp>-<urlhash>` and can be overridden via CLI arg or `KP_MANUAL_RUN_ID`.

## Prompt templates

Templates in `prompts/` use `{placeholder}` syntax:
- `tailor_job_analysis.md` — `{job_title}`, `{company}`, `{job_description}`
- `tailor_resume_plan.md` — `{job_analysis}`, `{candidate_profile_structured}`, `{positioning_rules}`
- `tailor_cover_letter_plan.md` — `{job_title}`, `{company}`, `{job_description}`, `{job_analysis}`, `{candidate_inventory}`, `{style_guide}`, `{positioning_rules}`

The cover letter prompt defines a **quality benchmark**: direct, concrete, no filler, 300–420 words, with 10 hard requirements including evidence grounding and banned-phrase rejection. Maintain this standard.

## Context folder (`pipelines/job_agent/context/`)

Reference materials for the Tailoring node. **Private files here are gitignored** (see root `.gitignore`); only `candidate_profile.example.yaml` ships in git — copy it to `candidate_profile.yaml` locally and edit. Repo-root `/context/` (gitignored) can hold bulk assets (PDFs, decks) if you prefer not to keep them under the pipeline path.

| File(s) | Purpose |
|---------|---------|
| `candidate_profile.yaml` | Structured profile data fed to prompts. **Primary input** for tailoring. Includes `cover_letter` section for voice/banned phrases. Local only. |
| `candidate_profile.example.yaml` | Committed template; safe to push. |
| `cover_letter_style.md` | Cover letter style guide with voice benchmark excerpts from Anduril, Human Agency, and CFS reference letters. |
| `Resume_Kokomoor_*.pdf/.docx` | Multiple resume versions for different positioning (defense-lead, tech-lead, startup-lead). Quality and voice benchmarks for generated resumes. |
| `Anduril.docx`, `CoverLetter_*.docx`, `Kokomoor_CoverLetter.docx` | Cover letter examples. Voice benchmarks excerpted in `cover_letter_style.md`. |
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
- Tailoring node expects `state.job_analyses` to be pre-populated by the upstream job-analysis node. If a listing has no matching analysis, it is skipped with an error — not crashed.
- Setting `KP_JOB_ANALYSIS_MODEL` to empty string means the analysis falls back to the default `KP_ANTHROPIC_MODEL` (Sonnet), which is more expensive. Only do this if Haiku quality is insufficient for a specific use case.
- `INDEED` is a supported `JobSource`. Provider detection, source mapping, and selectors all handle Indeed URLs.
- Missing `cover_letter` section in `candidate_profile.yaml` — all cover-letter quality knobs (banned phrases, positioning angles, narrative themes, signoff preference) are inert without it. The example YAML shows the expected shape.
- Using a free-text `tone_version` — it's a `Literal` type. Valid values: `confident_direct`, `professional_narrative`, `technical_precise`.
- Writing generic body paragraphs that cite bullet IDs in metadata but don't include specific terms from those bullets — prose grounding validation will reject the letter.
- Adding a core banned phrase to a cover letter test fixture — the validation now hard-rejects these. See `CORE_BANNED_PHRASES` in `cover_letter/validation.py`.
