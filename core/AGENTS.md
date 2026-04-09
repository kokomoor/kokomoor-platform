# AGENTS.md — core/

Shared infrastructure library. All pipelines import from this package. Changes here affect every pipeline.

## Subsystems

| Subpackage | Purpose | Key file(s) |
|------------|---------|-------------|
| `config.py` | Pydantic Settings, `KP_*` env vars | Single source of truth for all config |
| `database.py` | Async SQLModel engine + sessions | `AsyncEngine`, `async_sessionmaker`, SQLite/Postgres |
| `models/` | Shared base models | `TimestampMixin`, `BaseModel`, `PipelineRun` |
| `llm/` | LLM abstraction layer | See `core/llm/AGENTS.md` |
| `browser/` | Playwright lifecycle + stealth | `BrowserManager`, `stealth.py` |
| `fetch/` | Shared HTTP/browser HTML fetch + JSON-LD script parsing | `HttpFetcher`, `BrowserFetcher`, `ContentFetcher`, `jsonld.py` |
| `observability/` | Logging + metrics | `setup_logging()`, Prometheus counters |
| `notifications/` | Async SMTP | `send_notification()` |
| `testing/` | Test fixtures | `MockLLMClient`, `get_test_session()` |

## Rules

- **No pipeline-specific logic.** If it only matters to one pipeline, it belongs in that pipeline's package.
- **`core.fetch` is transport-only.** Keep it domain-agnostic: redirects, timeouts, retries, response status, and HTML retrieval belong here; job-board selectors and extraction heuristics do not.
- **All public APIs must be typed.** mypy strict is enabled project-wide.
- **Adding a new setting:** add to `Settings` in `config.py` with `KP_` prefix, default value, and description. Update `.env.example`.
- **Database changes:** use `async_sessionmaker` (not `sessionmaker`) with `AsyncEngine`. Create Alembic migrations for schema changes (`alembic revision --autogenerate -m "description"`).

## Common mistakes

- Using `sessionmaker` instead of `async_sessionmaker` — produces sync session types that fail at runtime or mypy.
- Importing provider SDKs (e.g. `anthropic`) outside of `core/llm/<provider>.py` — breaks the abstraction.
- Forgetting to update `MockLLMClient` in `testing/` when changing the `LLMClient` protocol.
- Adding pipeline models to `core/models/` — only shared bases belong here.

## Testing

Tests for `core/` live in `core/tests/`. Use fixtures from `core/testing/`:
- `get_test_session()` — in-memory SQLite, fresh tables per test
- `MockLLMClient` — canned responses, records calls, satisfies `LLMClient` protocol
