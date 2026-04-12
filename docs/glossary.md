# Glossary

## Platform concepts

| Term | Definition |
|------|-----------|
| **Pipeline** | A self-contained automation workflow under `pipelines/`. Has its own models, nodes, tests, and Dockerfile. |
| **Node** | A pure async function `(state) -> state` that performs one step of a pipeline. Registered in `graph.py`. |
| **State** | A typed dataclass (`JobAgentState`) that flows through the graph. Nodes read from and write to it. |
| **Graph** | A LangGraph `StateGraph` that wires nodes together with edges and conditional routing. Compiled into a `CompiledStateGraph` for execution. |

## Job agent phases

| Phase | What happens |
|-------|-------------|
| **Discovery** | Scrape job boards, parse listings, deduplicate against DB. |
| **Filtering** | Apply salary floor, keyword, and role filters. |
| **Job Analysis** | Dedicated LLM node: full JD → structured `JobAnalysisResult` (themes, qualifications, keywords, domain tags). Runs between extraction/filtering and tailoring. |
| **Tailoring** | Consumes pre-computed job analysis → tailoring plan → deterministic apply → `.docx` render. One LLM call (plan pass); the applier and renderer are pure code. |
| **Human Review** | Pause pipeline, notify owner, wait for approval. |
| **Application** | Fill and submit forms via Playwright. |
| **Tracking** | Persist listing states to database. |
| **Notification** | Send email digest of pipeline activity. |

## Model types

| Model | Base | Persisted? | Example |
|-------|------|-----------|---------|
| **SQLModel table** | `SQLModel, table=True` | Yes (DB) | `JobListing`, `PipelineRun` |
| **Pydantic model** | `pydantic.BaseModel` | No (transient) | `SearchCriteria`, `JobFilter` |
| **Resume tailoring model** | `pydantic.BaseModel` | No (transient) | `ResumeMasterProfile`, `JobAnalysisResult`, `ResumeTailoringPlan`, `TailoredResumeDocument` |

## Resume tailoring concepts

| Term | Definition |
|------|-----------|
| **Master profile** | YAML file (`candidate_profile.yaml`) with all possible resume content. Each bullet has a stable `id`, `tags`, and optional `variants` (short/long). |
| **Bullet op** | An operation applied to a single bullet: `keep` (use as-is), `shorten` (use short variant), or `rewrite` (LLM-provided replacement text). |
| **Tailoring plan** | LLM-generated structured plan specifying bullet selection, ordering, and ops for a specific job listing. |
| **Applier** | Pure function that resolves a tailoring plan against the master profile to produce a `TailoredResumeDocument`. No LLM calls. |

## LLM layer

| Term | Definition |
|------|-----------|
| **LLMClient** | `typing.Protocol` defining the interface any LLM provider must implement. |
| **AnthropicClient** | Production implementation of `LLMClient` using the Anthropic Messages API. |
| **MockLLMClient** | Test double in `core/testing/` that returns canned responses. Must satisfy `LLMClient` protocol. |
| **LLMUsage** | Dataclass tracking cumulative tokens, cost, errors, cache hits, and per-call logs. |
| **structured_complete** | Wrapper that requests JSON from the LLM and validates against a Pydantic model. |

## Discovery concepts

| Term | Definition |
|------|-----------|
| **ListingRef** | Minimal listing data from a search result card. Contains URL, title, company, location, and optional salary text. Converted to `JobListing` after deduplication and prefiltering. |
| **SessionStore** | Manages Playwright `storage_state` persistence per provider. Sessions survive between runs so established accounts don't get re-fingerprinted each time. |
| **HumanBehavior** | Behavioral simulation class for realistic Playwright interactions (mouse curves, reading pauses, typing cadence). All browser provider actions must use it. |
| **DomainRateLimiter** | Per-provider token bucket controlling navigation delays. LinkedIn uses 10-25s delays; Greenhouse (HTTP API) uses 0.5-2s. |
| **ProviderAdapter** | Protocol interface for job board adapters. Separates transport (browser vs HTTP) from the orchestration logic. |
| **BulkExtraction** | Post-filtering node that fetches full job descriptions for qualified listings before LLM analysis. Defers expensive page fetches until after the listing count is reduced by filtering. |
| **DiscoveryOrchestrator** | Coordinates all enabled providers (browser + HTTP), aggregates `ListingRef` results, and manages session lifecycle. |
| **DiscoveryConfig** | Pydantic model built from `KP_DISCOVERY_*` settings. Carries all discovery-specific config (enabled providers, concurrency, rate limits, target companies). Constructed via `DiscoveryConfig.from_settings()`. |
| **Prefilter** | Rule-based scoring of `ListingRef` metadata (title, location, salary) against `SearchCriteria`. Returns 0.0–1.0 score; listings below `prefilter_min_score` are dropped before `ref_to_job_listing()` conversion. No LLM involved. |
| **CaptchaHandler** | Detection and tiered response for CAPTCHAs (reCAPTCHA, hCaptcha, Cloudflare Turnstile/JS challenge). Three strategies: `avoid`, `pause_notify`, `solve`. |

## Scraper pipeline concepts

| Term | Definition |
|------|-----------|
| **SiteProfile** | Declarative configuration describing how to authenticate, navigate, and extract data from a target site. |
| **OutputContract** | Field-level schema and SLO expectations for scraper output, including dedup key composition. |
| **BaseSiteWrapper** | Generic profile-driven wrapper that handles auth, pagination, extraction, dedup, and drift checks. |
| **FixtureStore** | Captures and loads HTML/screenshot snapshots used for offline testing and drift baselines. |
| **StructuralFingerprint** | Token-efficient summary of DOM structure for drift detection between historical and live pages. |
| **RemediationReport** | Structured diagnosis output from heal flow, including root cause, affected files, and ordered remediation steps. |
| **HealToken** | Signed token included in diagnosis emails; required to authenticate IMAP \"fix\" replies. |

## Infrastructure

| Term | Definition |
|------|-----------|
| **`KP_*`** | Environment variable prefix for all platform settings (parsed by Pydantic Settings). |
| **BrowserManager** | Async context manager wrapping Playwright with stealth defaults, rate limiting, and session persistence. |
| **Stealth** | Anti-detection measures: two layers -- context-level (UA, viewport, timezone) and page-level (webdriver flag, plugins, WebGL, canvas fingerprint noise). |
| **Tower** | The deployment server (Ubuntu Server 24.04, accessed via Tailscale SSH). |
