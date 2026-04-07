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

## Infrastructure

| Term | Definition |
|------|-----------|
| **`KP_*`** | Environment variable prefix for all platform settings (parsed by Pydantic Settings). |
| **BrowserManager** | Async context manager wrapping Playwright with stealth defaults and rate limiting. |
| **Stealth** | Anti-detection measures: randomized user agents, viewports, timezones, human-realistic delays. |
| **Tower** | The deployment server (Ubuntu Server 24.04, accessed via Tailscale SSH). |
