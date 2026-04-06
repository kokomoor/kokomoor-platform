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
| **Tailoring** | Generate customized resume + cover letter via LLM. |
| **Human Review** | Pause pipeline, notify owner, wait for approval. |
| **Application** | Fill and submit forms via Playwright. |
| **Tracking** | Persist listing states to database. |
| **Notification** | Send email digest of pipeline activity. |

## Model types

| Model | Base | Persisted? | Example |
|-------|------|-----------|---------|
| **SQLModel table** | `SQLModel, table=True` | Yes (DB) | `JobListing`, `PipelineRun` |
| **Pydantic model** | `pydantic.BaseModel` | No (transient) | `SearchCriteria`, `JobFilter` |

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
