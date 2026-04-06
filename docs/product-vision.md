# Product Vision

## What this is

A personal **agentic pipeline platform**. Shared infrastructure (`core/`) powers self-contained automation pipelines (`pipelines/`). The first pipeline automates job search; future pipelines (ML showcase, portfolio management) reuse the same core.

## Who it serves

- **Primary user:** the repo owner — automating job discovery, resume/cover letter tailoring, and application tracking.
- **Secondary audience:** hiring managers and senior engineers reviewing this repo as a portfolio artifact. Code quality, observability, and architecture matter as much as functionality.

## What success looks like

- The job agent discovers listings, filters by criteria, tailors materials to the owner's voice, pauses for human approval, and submits applications — all with structured logging and cost tracking.
- Adding a new pipeline means creating a folder under `pipelines/`, importing from `core/`, and writing nodes. No new infrastructure.

## Hard constraints

- **Never auto-submit applications.** A human approval gate before any submission is non-negotiable.
- **Anti-detection is mandatory.** Browser automation must use stealth defaults, rate limiting, and realistic timing. Raw Playwright without `BrowserManager` is not acceptable.
- **Single-user, single-server.** No multi-tenancy, no cloud services, no distributed systems. Runs on one Ubuntu box via Docker Compose.
- **Portfolio quality.** Strict typing (mypy), linting (ruff), structured logging, test coverage, and Alembic migrations from day one.

## Non-goals

- Not a SaaS product or framework for others.
- Not a distributed system — no Airflow, Lambda, or message queues.
- Not a general-purpose LLM framework — `core/llm/` is an abstraction for this platform's needs.
