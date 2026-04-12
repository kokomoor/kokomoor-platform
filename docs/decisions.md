# Architectural Decisions

Short records of key choices and their rationale. Reference these when asking "why is it done this way?"

---

### D1: Monorepo over polyrepo

**Choice:** Single repo with `core/` + `pipelines/`.
**Why:** Shared infrastructure changes deploy atomically with pipeline changes. One CI pipeline. Simpler dependency management. The cost (larger repo) is negligible at this scale.

### D2: LangGraph for orchestration

**Choice:** LangGraph state machine over custom DAG or Airflow.
**Why:** Nodes are pure `(state) -> state` functions. LangGraph handles checkpointing, conditional edges, and async execution. Avoids building a custom orchestrator. Good fit for agentic workflows with human-in-the-loop.

### D3: SQLite default, Postgres-ready

**Choice:** SQLite via aiosqlite now; Postgres via connection string swap later.
**Why:** Zero-config for development and single-server deployment. SQLModel + Alembic ensure the schema is portable. Migration to Postgres requires only changing `KP_DATABASE_URL`.

### D4: LLMClient as Protocol, not ABC

**Choice:** `typing.Protocol` with `@runtime_checkable` over abstract base class.
**Why:** Structural typing — implementations don't need to inherit from anything. `MockLLMClient` in tests satisfies the protocol without import coupling. Adding a new provider means writing a class with the right shape, not subclassing.

### D5: Playwright with stealth over API-only scraping

**Choice:** Playwright browser automation with anti-detection defaults.
**Why:** Job boards require JavaScript rendering. Application submission requires form-fill. API-only approaches don't cover these. Stealth (randomized fingerprints, rate limiting, human-realistic delays) is mandatory to avoid detection.

### D6: structlog over stdlib logging

**Choice:** structlog with JSON rendering in production, console in dev.
**Why:** Structured key-value pairs are searchable and parseable. Context binding (`logger.bind(run_id=...)`) threads identifiers through call chains. JSON output integrates with log aggregation.

### D7: Single-server deployment

**Choice:** Run everything on one Ubuntu box via Docker Compose. No cloud services.
**Why:** Simplicity and cost. A personal automation tool doesn't need distributed infrastructure. The tower (Ubuntu Server 24.04, Tailscale SSH) is the deployment target.

### D8: Multi-phase LLM pipeline over single-prompt generation

**Choice:** Two LLM passes in separate graph nodes (job analysis node → tailoring plan node) plus deterministic code (apply → render), instead of one large prompt that generates a full resume.
**Why:** Cheaper (small structured JSON outputs vs full document text). More controllable — facts live in the master profile YAML, not in LLM output. Layout is owned by code (python-docx), not negotiated with the model. The applier and renderer are pure functions, fully testable without API calls. Separating analysis into its own node ensures the full JD is processed (no truncation), makes analysis results reusable by future nodes (cover letter, scoring), and allows independent model/token configuration per phase.

### D9: Code-based .docx rendering over template file

**Choice:** Build resume documents programmatically with python-docx rather than filling a committed `.docx` template.
**Why:** Fully deterministic -- styling is defined in code, no binary files in git. Easier to test (no template file dependency). The `KP_RESUME_TEMPLATE_PATH` setting is reserved for a future user-supplied template if needed.

---

### D10: Discovery emits ListingRef, not full JobListing

**Choice:** Discovery collects minimal card data (`ListingRef`); `bulk_extraction_node` fetches full descriptions after filtering.
**Why:** Avoids fetching full pages for listings that will be filtered by salary/keywords. Filtering on card metadata (title, company, partial salary) is sufficient to cut 60-80% of listings before the expensive full-page fetch. This keeps session exposure time low and reduces risk of bot detection.

### D11: Browser sessions are persisted and reused

**Choice:** `data/sessions/<provider>.json` stores Playwright `storage_state` between runs.
**Why:** Established sessions with cookies and browsing history are dramatically less likely to trigger bot detection than fresh contexts. LinkedIn specifically requires session warmup over multiple days before reliable operation. The session directory is gitignored.

### D12: Two-tier provider architecture within one node

**Choice:** Browser and HTTP providers coexist in the same discovery node, coordinated by `DiscoveryOrchestrator`.
**Why:** Greenhouse and Lever have excellent public APIs -- using a browser for them would be slower and increase detection risk unnecessarily. The protocol-based `ProviderAdapter` allows both tiers to be coordinated by the same orchestrator. A future API pipeline (LinkedIn Jobs API, Indeed Publisher API, etc.) will require approved partner access; these adapters will be migrated when access is granted.

### D13: Semaphore-bounded concurrent providers

**Choice:** `asyncio.Semaphore(max_concurrent_providers)` inside `asyncio.gather`, not unbounded gather.
**Why:** Each browser provider opens a Chromium process with distinct fingerprint and session. Running all of them simultaneously from a single IP triggers IP-level rate limiting and cross-session correlation at CDN/WAF layers. Bounding concurrency (default 2) keeps the IP footprint realistic. The semaphore is acquired inside each task, so `gather` still dispatches all tasks but only N execute at any time.

### D14: CAPTCHA tiered strategy

**Choice:** Three-tier CAPTCHA handling: `avoid` (skip provider immediately), `pause_notify` (wait + alert human), `solve` (automated 2captcha).
**Why:** CAPTCHAs indicate detection — solving one doesn't fix the underlying signal. The `avoid` tier prevents wasted browser time. `pause_notify` lets a human intervene (manual solve, session refresh) which is the safest response. `solve` is the last resort for providers where automated solving is reliable (primarily Cloudflare Turnstile). Default is `pause_notify` because most CAPTCHAs on job boards indicate session/fingerprint problems that automated solving won't resolve long-term.

### D15: Universal scraper as profile + wrapper architecture

**Choice:** Introduce `pipelines/scraper` with a strict split between declarative `SiteProfile` YAML and optional site-specific wrapper subclasses.
**Why:** Most sites can run on the generic base wrapper with profile-only config. Complex targets (ASP.NET postback, vendor-specific portals) stay isolated in wrappers without polluting `core/`.

### D16: Shared scraper primitives live in `core/scraper`

**Choice:** Move dedup, content storage, fixture capture/fingerprinting, and HTTP-first transport into domain-agnostic `core/scraper`.
**Why:** These capabilities are reusable infrastructure across pipelines and keep pipeline code focused on site behavior and business logic.

### D17: Signed human-gated heal trigger

**Choice:** Require a signed `Heal Token` in IMAP replies before remediation can be triggered.
**Why:** Reply content alone (`fix`) is insufficient authentication. Signed tokens with TTL prevent spoofed and replayed triggers while preserving human-in-the-loop control.

### D18: Process-local IMAP replay prevention (accepted risk)

**Choice:** Heal reply replay prevention uses an in-process `set` of processed message IDs, not a persistent store.
**Why:** Three independent layers mitigate replay risk without persistence overhead: (1) Token TTL (default 24h) limits the temporal attack window. (2) IMAP `\Seen` flag prevents the same message from appearing in `UNSEEN` searches again. (3) `was_already_attempted()` in the heal node prevents re-execution of a given `heal_id`. The process-local set is a fast-path optimization that prevents re-processing within a single watcher session; it is not the sole defense. If persistent replay tracking becomes necessary (e.g., multi-process deployment), migrate `_processed_message_ids` to SQLite keyed by `Message-ID`.
