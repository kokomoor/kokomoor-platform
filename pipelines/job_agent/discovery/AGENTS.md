# AGENTS.md — pipelines/job_agent/discovery/

Browser-based and HTTP-based job listing collection. Self-contained subsystem under the job agent pipeline.

## Purpose and boundaries

This package owns everything between "the user's search criteria" and "a deduplicated list of `JobListing` objects ready for filtering." It does **not** own filtering, analysis, tailoring, or application logic — those live in sibling nodes.

No pipeline-specific logic from other nodes belongs here. No `core/`-level changes should be needed for discovery features — use `BrowserManager`, `core.fetch`, and `core.config` as provided.

## File map

| File | Role | Status |
|------|------|--------|
| `__init__.py` | Package marker | Implemented |
| `models.py` | `ListingRef`, `ParsedSalary`, `ProviderResult`, `DiscoveryConfig`, `ref_to_job_listing()`, `parse_salary_text()` | Implemented |
| `session.py` | `SessionStore` — per-provider browser session persistence | Implemented |
| `rate_limiter.py` | `DomainRateLimiter`, `PROVIDER_LIMITS` — per-domain async rate limiting | Implemented |
| `url_utils.py` | `canonicalize_url()`, domain-specific URL normalization, LinkedIn ID extraction | Implemented |
| `deduplication.py` | `compute_dedup_key()`, `deduplicate_refs()` — in-run + DB dedup | Implemented |
| `prefilter.py` | `score_listing_ref()`, `apply_prefilter()` — rule-based fit scoring | Implemented |
| `orchestrator.py` | `DiscoveryOrchestrator` — fan-out to enabled providers, aggregate results | Implemented |
| `providers/__init__.py` | Package marker, re-exports `ProviderAdapter` | Implemented |
| `providers/protocol.py` | `ProviderAdapter` — `@runtime_checkable` Protocol | Implemented |
| `providers/base.py` | `BaseProvider` — abstract browser adapter with pagination loop | Implemented |
| `providers/linkedin.py` | `LinkedInProvider` — LinkedIn search scraper with feed warm-up, auth flow, dual pagination | Implemented |
| `providers/indeed.py` | `IndeedProvider` — Indeed browser scraper with dual-selector pagination | Implemented |
| `providers/builtin.py` | `BuiltInProvider` — Built In browser scraper with city-edition URL mapping | Implemented |
| `providers/wellfound.py` | `WellfoundProvider` — Wellfound browser scraper with email/password auth | Implemented |
| `providers/greenhouse.py` | `GreenhouseProvider` + `fetch_all_greenhouse_companies()` — Greenhouse boards HTTP API | Implemented |
| `providers/lever.py` | `LeverProvider` + `fetch_all_lever_companies()` — Lever postings HTTP API | Implemented |
| `providers/workday.py` | `WorkdayProvider` — Workday ATS scraper with per-company iteration, Load More pagination | Implemented |
| `providers/direct_site.py` | `DirectSiteProvider` — YAML-configured proprietary career site scraper | Implemented |
| `human_behavior.py` | `HumanBehavior` — realistic scroll, click, type, mouse movement | Implemented |
| `captcha.py` | `CaptchaHandler` — detection + tiered response (wait/notify/solve) | Implemented |
| `AGENTS.md` | This file | Implemented |

## Data flow

```
SearchCriteria
  → DiscoveryConfig.from_settings()
  → Orchestrator (fan-out to enabled providers, bounded by max_concurrent_providers)
    → ProviderAdapter.search() per provider
      → ListingRef list per provider
    ← ProviderResult per provider
  → Aggregate all ListingRef lists
  → Deduplicate (by dedup_key: sha256 of company|title|url)
  → Prefilter (rule-based fit score ≥ prefilter_min_score)
  → ref_to_job_listing() per ListingRef
  → list[JobListing] (status=DISCOVERED)
```

## ProviderAdapter protocol contract

Every provider must implement:

```python
@runtime_checkable
class ProviderAdapter(Protocol):
    source: ClassVar[JobSource]

    def requires_auth(self) -> bool: ...
    def base_domain(self) -> str: ...
    async def is_authenticated(self, page: Page) -> bool: ...
    async def authenticate(
        self, page: Page, *, email: str, password: str, behavior: HumanBehavior,
    ) -> bool: ...
    async def run_search(
        self, page: Page, criteria: SearchCriteria, config: DiscoveryConfig, *,
        behavior: HumanBehavior, rate_limiter: DomainRateLimiter, captcha_handler: CaptchaHandler,
    ) -> list[ListingRef]: ...
```

The orchestrator instantiates a `BrowserManager` per browser provider, creates a `Page`, and calls `run_search()`. HTTP providers (Greenhouse, Lever) receive `None` for the `page` and browser-specific kwargs. Providers should capture errors internally and return an empty list rather than raising.

## Anti-detection invariants

These are **hard rules** — violating any of them will get the pipeline blocked:

1. **All browser providers use `BrowserManager`**, never raw Playwright. This ensures context-level stealth (UA, viewport, timezone) is always applied.
2. **All pages get page-level stealth injection automatically** via `BrowserManager.new_page()`. Do not call `apply_page_stealth()` manually.
3. **`DomainRateLimiter.wait()` is called before every page navigation.** This is in addition to `BrowserManager.rate_limited_goto()` (which is a global floor). The domain limiter adds provider-specific delays and periodic long pauses.
4. **Human behavior methods are used for all interactive page actions** — `scroll_down_naturally()` for scrolling, `human_click()` for clicking, `type_with_cadence()` for typing. Never use raw `page.click()` without jitter/delay.
5. **`SessionStore` must be used** — never start a browser provider with an empty context if a valid (fresh) session file exists. Always attempt to restore the session first.

## Indeed provider specifics

The Indeed provider (`providers/indeed.py`) has scraper-specific notes worth documenting:

- **Search URLs**: `https://www.indeed.com/jobs?q={query}&l={location}&fromage=14&sc=0kf%3Aattr(DSQF7)%3B`. The `fromage=14` filter limits to last 14 days; `DSQF7` filters for full-time positions.
- **Card selectors**: Primary `[data-testid='slider_item']`, fallback `.job_seen_beacon`. Both are queried; results are deduped by job key (`jk`).
- **Job key (`jk`)**: Extracted from `a[data-jk]` within the card or `data-jk` on the card element. This is the unique job identifier on Indeed.
- **Canonical URL**: Built as `https://www.indeed.com/viewjob?jk={jk}` — never use card link `href` values (they contain tracking params). `canonicalize_url()` strips all but `jk` for Indeed URLs.
- **Salary extraction**: `[data-testid='attribute_snippet_testid']` or `.salary-snippet-container`. May be absent; returns empty string when missing.
- **Pagination**: Dual-selector next button — `[data-testid='pagination-page-next']` primary, `a[aria-label='Next Page']` fallback. The provider overrides `_run_single_search` to check both selectors instead of relying on the base `_next_page_selector()` method.
- **Rate limits**: 5-14s between pages, long pause every 8 pages (30-75s). See `PROVIDER_LIMITS[JobSource.INDEED]`.

## LinkedIn provider specifics

The LinkedIn provider (`providers/linkedin.py`) is the **highest-risk** provider — LinkedIn has the most sophisticated bot detection of any job board. It is the only provider where a mistake in behavioral timing or selector use can lead to an account restriction.

### Session persistence (mandatory)

- A fresh session (no cookies) triggers CAPTCHA within 1-2 page loads. `SessionStore` must be used — never launch LinkedIn without a valid session file if one exists.
- Sessions are reliable for 48-72 hours. After that, re-authentication may be required.
- First-time sessions require manual CAPTCHA/email verification. The `CaptchaHandler` will notify the owner via `pause_notify`.

### Feed warm-up

`run_search()` navigates to `https://www.linkedin.com/feed/` before any search. This mimics a human returning to LinkedIn and browsing the feed before job searching. The warm-up includes `reading_pause`, `scroll_down_naturally`, and an extended `between_actions_pause` (2-5s).

### Authentication flow

1. Navigate to `https://www.linkedin.com` (not `/login` directly — landing on home first is human behavior).
2. Find and click the "Sign in" link using `human_click`.
3. Fill email and password using `type_with_cadence` with pauses between fields.
4. `hover_before_click` on submit before clicking (humans hover before committing).
5. Check for CAPTCHA and email verification pin challenges after submission.
6. Verify login success via nav avatar selectors.

Selectors (may change): `input#username`, `input#password`, `button[type='submit']`.

### URL construction

- `https://www.linkedin.com/jobs/search/?keywords={kw}&location={loc}&f_TPR=r604800{remote}&sortBy=DD`
- `f_TPR=r604800` = past week (604800 seconds). Always applied.
- `f_WT=2` = remote only. Applied only when `criteria.remote_ok` is True.
- `sortBy=DD` = date descending (newest first). Always applied.
- Keywords: `target_roles` preferred (one URL per role, max 3 words each). Falls back to `keywords` joined. Default fallback: `"software engineer"`.
- Locations: each entry in `criteria.locations`, capped at 3. Default: `"United States"`.
- Hard cap: **6 search URLs** total. More is not better — it extends session exposure time.

### Card extraction selectors

- Results container: `.jobs-search__results-list` primary, `[data-test='job-search-results']` or `.job-card-container` fallback.
- Job ID: `data-entity-urn="urn:li:job:{id}"` primary, `data-job-id` fallback, then `href` containing `/jobs/view/{id}/`.
- Title: `.job-card-list__title`, `.job-card-container__link`, or `aria-label` on `<a>`.
- Company: `.job-card-container__company-name`, `.artdeco-entity-lockup__subtitle`, `.job-card-list__company-name`.
- Location: `.job-card-container__metadata-item` filtered to items containing location hints (comma, "Remote", "hybrid", city names).
- Salary: `.job-card-container__metadata-item` containing `$` or `K`.
- **Canonical URL**: Always `https://www.linkedin.com/jobs/view/{job_id}/` — **never** use card `href` (contains tracking params that identify scrapers).

### Pagination

- Primary: `button[aria-label="View next page"]` clicked via `human_click`.
- Fallback: numbered pagination `li.artdeco-pagination__indicator--number[aria-current='true'] + li button`.
- Last resort: infinite scroll — scroll to bottom, wait 2s for content load.
- Rate limits: **10-25s between pages, 45-120s long pause every 5 pages**. See `PROVIDER_LIMITS[JobSource.LINKEDIN]`.

## Provider implementation checklist

When adding a new provider:

- [ ] Create `providers/<name>.py` implementing the `ProviderAdapter` protocol
- [ ] Add a `JobSource` enum value if not already present (in `pipelines/job_agent/models/__init__.py`)
- [ ] Add a `DomainRateLimit` entry in `rate_limiter.py` → `PROVIDER_LIMITS`
- [ ] Add enable flag to `DiscoveryConfig` and `Settings` (`KP_DISCOVERY_<NAME>_ENABLED`)
- [ ] Wire into the orchestrator's provider dispatch
- [ ] Add unit tests with mocked pages / HTTP responses (no real network)
- [ ] Respect all five anti-detection invariants above
- [ ] Handle pagination up to `config.max_pages_per_search`
- [ ] Respect `config.max_listings_per_provider` hard cap
- [ ] Save session via `SessionStore.save()` on successful completion
- [ ] Return errors in `ProviderResult.errors`, never raise from `search()`
