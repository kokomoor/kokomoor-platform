# AGENTS.md — core/fetch/

Shared **transport** for fetching HTML. No pipeline- or domain-specific scraping
logic belongs here.

## Contents

| Module | Role |
|--------|------|
| `types.py` | `FetchResult`, `FetchMethod` |
| `protocol.py` | `ContentFetcher` protocol |
| `http_client.py` | `HttpFetcher` — httpx, retries, timeouts, logging |
| `browser_fetch.py` | `BrowserFetcher` — `BrowserManager` + `page.content()` |
| `jsonld.py` | Parse `application/ld+json` script bodies (generic JSON, not JobPosting-specific) |

## Rules

- **Pipelines** implement specialized extractors (selectors, scoring, mapping to
  domain models) and call `HttpFetcher` / `BrowserFetcher` or alternate
  `ContentFetcher` implementations.
- **Do not** add job-board selectors, ATS-specific parsing, or LLM calls here.
- New settings use `KP_FETCH_*` in `core/config.py`.

## Testing

Tests live in `core/tests/test_fetch.py`. Use `respx` for HTTP mocks; mock
`BrowserManager` if browser behavior must be asserted without Playwright.
