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
**Why:** Fully deterministic — styling is defined in code, no binary files in git. Easier to test (no template file dependency). The `KP_RESUME_TEMPLATE_PATH` setting is reserved for a future user-supplied template if needed.
