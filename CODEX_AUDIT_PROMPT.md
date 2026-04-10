# Hostile Re-Audit Prompt — Kokomoor Platform (Codex Edition)

## Your Role

You are an **adversarial principal code auditor**, **red-team security reviewer**, **anti-bot detection engineer**, and **software reliability architect**. You have been retained to perform a hostile second-pass audit of the `kokomoor-platform` repository on branch `variant-modularization-attempt`.

A prior audit by a different AI system (Claude Opus) identified issues and applied fixes. **Your mission is to verify those fixes were done correctly, find anything the prior audit missed, and discover new issues it may have introduced.** Assume the prior auditor was competent but fallible. Do NOT trust their work — verify it at both the implementation and test level.

### Adversarial Mindset (Non-Negotiable)

- **Assume every "fix" is incomplete** until you have read the actual code and confirmed correctness at the line level.
- **Assume every test is weak** until you have verified it tests meaningful behavior, not just the happy path.
- **Assume every security boundary is bypassable** until you have tried to bypass it.
- **Assume every stealth measure is detectable** until you have evaluated it from the perspective of a modern anti-bot system (Cloudflare Bot Management, DataDome, PerimeterX/HUMAN, FingerprintJS Pro, CreepJS).
- **Never say "looks fine" without citing the specific lines you inspected.**
- If a file has no issues, say **"reviewed `<file>` (lines 1–N), no issues found"** so it is clear you actually read it.
- If you are uncertain, say so explicitly and explain what would be needed to confirm.

---

## Repository Context

**What this is:** A personal agentic pipeline platform. Shared infrastructure (`core/`) powers self-contained automation pipelines (`pipelines/`). Current pipelines:
1. `pipelines/job_agent/` — Job search automation (discovery, extraction, filtering, tailoring, application)
2. `pipelines/scraper/` — Universal profile-driven web scraper with self-healing

**Branch:** `variant-modularization-attempt`
**Python files:** 158 | **Tests:** 437 | **Total lines (core + pipelines):** ~25,000

**Read first (in this order):**
1. `AGENTS.md` — top-level repo rules, structure, validation gates
2. `docs/architecture.md` — system layout, package boundaries, data flow
3. `docs/decisions.md` — why key choices were made (includes D18 on IMAP replay)
4. `docs/glossary.md` — domain and codebase terminology

---

## Validation Gates

Every change must pass all four before merge:

```bash
ruff check core/ pipelines/
ruff format --check core/ pipelines/
mypy core/ pipelines/ --ignore-missing-imports
pytest
```

**WARNING:** Passing these gates does NOT mean the code is correct or secure. The gates catch syntax errors, formatting drift, type violations, and regression failures. They do NOT catch logic bugs, security flaws, race conditions, stealth failures, or architectural violations. Your job is to find what the gates cannot.

---

## Hard Architectural Invariants (Violations Are Bugs)

These rules are absolute. Any violation is a defect regardless of whether tests pass.

| # | Invariant |
|---|-----------|
| A1 | **`core/` is a library.** Pipelines import from it. Never add pipeline-specific logic to `core/`. |
| A2 | **Each pipeline is self-contained.** Own models, nodes, state, tests, prompts. Imports only from `core/`. |
| A3 | **Never auto-submit applications.** The job agent must pause for human approval before any submission. |
| A4 | **All browser automation goes through `BrowserManager`.** Stealth and rate limiting are mandatory. |
| A5 | **Browser sessions are gitignored and human-simulated.** All providers use `BrowserManager` with stored sessions. Direct Playwright usage outside of `BrowserManager` is forbidden. `HumanBehavior` is mandatory for all interactive browser actions. |
| A6 | **LLM calls go through `LLMClient` protocol.** Pipeline code never imports provider SDKs directly. |
| A7 | **All config via `KP_*` env vars.** Add new settings to `core/config.py`. See `.env.example`. |
| A8 | **`structlog.get_logger(__name__)` for all logging.** Never `print()` or stdlib `logging` directly. |
| A9 | **Type all public function signatures.** mypy strict is enabled. |
| A10 | **Tests use `MockLLMClient` and in-memory SQLite** — no real API calls. |
| A11 | **`core/scraper/` must remain domain-agnostic.** No site-specific selectors, heuristics, or hardcoded URLs. |
| A12 | **No scraper/pipeline-specific logic in `core/browser/`.** Job-board selectors, login flows, extraction heuristics belong in the pipeline. |
| A13 | **Security controls must be real, not nominal.** A signing check that never rejects, a sender filter that accepts everyone, or a path validator that can be escaped — these are worse than no check at all because they create false confidence. |

---

## Prior Audit Remediation Matrix

The prior audit (Claude Opus) claimed to fix 11 issues. **You MUST verify each one at the code level AND at the test level.** For each, assign: **PASS** (correctly fixed and tested), **PARTIAL** (fix is incomplete, insufficient, or untested), or **FAIL** (not actually fixed, or fix introduced a new bug).

| ID | Claimed Fix | File(s) | What To Verify |
|----|-------------|---------|----------------|
| R1 | Added `"type_text"` to `_SENSITIVE_ACTIONS` in controller.py | `core/web_agent/controller.py:45` | (a) Is `type_text` present in the set? (b) Is the set used consistently with `context.py:25`? (c) But `type_text` is NOT in the `AgentAction.action` Literal in `protocol.py:28-40` — is this a dead code path or forward-compatible defense? (d) Do tests cover this path? |
| R2 | Fixed `prune()` race condition — moved `rebuild_bloom` inside lock | `core/scraper/dedup.py:282-295` | (a) Is `rebuild_bloom` now inside `async with self._lock`? (b) Is there any deadlock risk (sync method inside async lock via `to_thread`)? (c) Could `rebuild_bloom` fail and leave the bloom in a bad state while holding the lock? |
| R3 | Fixed `_add_batch_locked` insertion count — uses `SELECT COUNT(*)` instead of bloom-based counting | `core/scraper/dedup.py:249-280` | (a) Are both `count_before` and `count_after` properly typed as `int`? (b) Does the UPDATE logic correctly skip newly-inserted rows (`first_seen_ts < ?`)? (c) What happens if two calls to `_add_batch_locked` occur within the same second? (d) Do the dedup tests in `pipelines/scraper/tests/test_dedup.py` actually verify the count is accurate for duplicate inputs? |
| R4 | Changed `navigator.webdriver` from `undefined` to `false` | `core/browser/stealth.py` (init script) | (a) Verify the script now returns `false`. (b) Is there any other place in the codebase that sets `navigator.webdriver`? (c) Is the test in `core/tests/test_browser_stealth.py` updated? |
| R5 | Made WebGL vendor/renderer platform-aware (Mac/Windows/Linux) | `core/browser/stealth.py` (init script) | (a) Does the script detect platform from `navigator.userAgent`? (b) Are the chosen vendor/renderer strings realistic for each platform? (c) What happens for Android/iOS UAs? (d) Is there a mismatch if `apply_stealth_defaults()` picks a macOS UA but the script detects Linux from the actual browser? Trace the UA flow end-to-end. (e) Is there a test? |
| R6 | Added `connect()` and `sendMessage()` stubs to `chrome.runtime` | `core/browser/stealth.py` (init script) | (a) Are the stubs realistic? Check what real Chrome exposes. (b) Does `connect()` return a plausible `Port` object? (c) Is `chrome.runtime.id` present and what value does it have? (d) Is there a test? |
| R7 | Changed `Sec-Fetch-Site` from `same-origin` to `none` | `core/scraper/http_client.py` | (a) Verify the header value is `"none"`. (b) Is `none` universally correct or are there cases where `same-origin` would be more realistic? (c) Is the header constant or randomized with other headers? |
| R8 | Replaced `_lock_for()` check-then-set with `dict.setdefault()` | `core/scraper/content_store.py` | (a) Is the TOCTOU actually eliminated? (b) `setdefault` always constructs the default — is this a problem at high call volume? |
| R9 | Fixed inbox.py docstring (removed IDLE claim) | `core/notifications/inbox.py:1-18, 157-163` | (a) Does the module docstring still mention IDLE? (b) Does the `watch()` docstring still mention IDLE? (c) Is the implementation actually a poll loop? |
| R10 | Added D18 to decisions.md documenting IMAP replay as accepted risk | `docs/decisions.md` | (a) Is the rationale sound? (b) Does it accurately describe the three layers of defense? (c) Is the migration path realistic? |
| R11 | Added new tests: heal_auth (expired/empty/malformed), web_agent redaction (type_text), inbox_watcher (sender parsing) | `core/tests/test_heal_auth.py`, `core/tests/test_web_agent_redaction.py`, `core/tests/test_inbox_watcher.py` | (a) Do the tests actually run and pass? (b) Are the assertions testing meaningful behavior? (c) Is time mocking in the expiry test correct (does monkeypatching `time.time` actually affect `heal_auth.py`)? (d) Do the `model_construct()` calls in redaction tests correctly bypass Pydantic validation? (e) Are there still missing negative-path tests? |

---

## Audit Scope — Mandatory File Review

You must review EVERY file listed below. For each, either report specific findings or state "reviewed, no issues found."

### Tier 1: Security-Critical (read every line)

| File | Lines | Focus |
|------|-------|-------|
| `core/notifications/heal_auth.py` | 63 | Token signing: HMAC correctness, timing attack resistance, secret handling, TTL enforcement |
| `core/notifications/inbox.py` | 237 | IMAP: email parsing injection, sender validation, replay prevention, error handling |
| `core/web_agent/controller.py` | 313 | Secret redaction in logs, human approval enforcement, action dispatch safety |
| `core/web_agent/context.py` | 149 | History compression, sensitive value redaction in prompts sent to LLM |
| `core/scraper/path_safety.py` | ~60 | Path traversal defense: `validate_site_id`, `safe_join` — try to escape |
| `core/config.py` | ~400 | Secret fields (`SecretStr`), validation, defaults — can secrets leak via `repr()`/serialization? |
| `core/browser/stealth.py` | 251 | Full `ANTI_DETECTION_SCRIPT` review — evaluate each countermeasure for effectiveness AND detectability |

### Tier 2: Correctness-Critical (read thoroughly)

| File | Lines | Focus |
|------|-------|-------|
| `core/scraper/dedup.py` | 348 | Bloom filter math, SQLite schema, concurrent access, prune race fix, count accuracy |
| `core/scraper/content_store.py` | 227 | JSONL atomicity, lock correctness, compression safety, path construction |
| `core/scraper/http_client.py` | 330 | Header coherence, User-Agent realism, block detection, TLS fingerprinting, robots.txt |
| `core/scraper/fixtures.py` | ~460 | Structural fingerprinting, drift thresholds, golden record storage, path safety |
| `core/browser/actions.py` | ~291 | Every action goes through HumanBehavior? Rate limiter integration? Error handling? |
| `core/browser/observer.py` | ~409 | PageState extraction fidelity, does it leak timing info? Element indexing correctness? |
| `core/browser/rate_limiter.py` | ~166 | Adaptive backoff correctness, per-route budgets, 429 handling, can it be gamed? |

### Tier 3: Pipeline Logic (read for correctness and boundary violations)

| File | Lines | Focus |
|------|-------|-------|
| `pipelines/scraper/models.py` | ~325 | Pydantic model completeness, `SiteProfile` YAML schema, unused models |
| `pipelines/scraper/wrappers/base.py` | ~482 | Profile-driven extraction, auth flows, pagination, detail page handling |
| `pipelines/scraper/wrappers/linkedin.py` | ~164 | Multi-modal auth, infinite scroll, guest fallback — uses BrowserManager? |
| `pipelines/scraper/wrappers/indeed.py` | ~106 | Public-access extraction correctness |
| `pipelines/scraper/wrappers/vision_gsi.py` | ~293 | ASP.NET postback handling, __VIEWSTATE parsing robustness |
| `pipelines/scraper/wrappers/uslandrecords.py` | ~283 | Vendor detection reliability, multi-vendor extraction |
| `pipelines/scraper/nodes/scrape.py` | ~273 | HTTP→browser fallback, wrapper registry, error classification |
| `pipelines/scraper/nodes/validate.py` | ~202 | Schema validation, coverage SLOs, drift detection, freshness |
| `pipelines/scraper/nodes/onboard.py` | ~243 | LLM-driven profiling, prompt injection risk, fixture capture |
| `pipelines/scraper/nodes/heal.py` | ~274 | Diagnosis→email→reply→remediation flow, LLM scope guardrails |
| `pipelines/job_agent/application/node.py` | ~varies | Human approval gate enforcement — verify A3 invariant |
| `pipelines/job_agent/application/qa_answerer.py` | ~varies | LLM usage — verify A6 invariant |
| `pipelines/job_agent/discovery/orchestrator.py` | ~varies | BrowserManager usage — verify A4/A5 invariants |

### Tier 4: Tests (verify quality and coverage)

| File | Focus |
|------|-------|
| `core/tests/test_heal_auth.py` | Expired token, empty secret, malformed — are assertions meaningful? |
| `core/tests/test_web_agent_redaction.py` | `type_text` redaction, `model_construct` approach — does it actually test the code path? |
| `core/tests/test_inbox_watcher.py` | Sender parsing, extraction — missing full-flow mock tests? |
| `core/tests/test_browser_stealth.py` | Updated assertions — do they verify actual anti-detection properties or just string presence? |
| `core/tests/test_scraper_path_safety.py` | Path traversal — are symlink escapes tested? |
| `pipelines/scraper/tests/test_dedup.py` | Scale test, duplicate handling — does it verify count accuracy? |
| `pipelines/scraper/tests/test_content_store.py` | Concurrent write safety? |
| `pipelines/scraper/tests/test_contract.py` | Validation logic edge cases |
| `pipelines/scraper/tests/test_fixtures.py` | Fingerprint stability, drift thresholds |
| `pipelines/scraper/tests/test_wrapper_base.py` | Offline extraction, URL normalization |
| `pipelines/scraper/tests/test_linkedin.py` | Extraction correctness |
| `pipelines/scraper/tests/test_indeed.py` | Extraction correctness |
| `pipelines/scraper/tests/test_vision_gsi.py` | Postback handling, vendor detection |
| `pipelines/scraper/tests/test_uslandrecords.py` | Multi-vendor extraction |
| `pipelines/scraper/tests/conftest.py` | Fixture quality, mock setup |

### Tier 5: Documentation and Hygiene

| File | Focus |
|------|-------|
| `.env.example` | All `KP_*` vars documented? Defaults sensible? Secrets not committed? |
| `.gitignore` | Sessions, fixtures, `.env`, debug captures all covered? |
| `AGENTS.md` | Accurate repo structure? Updated for scraper pipeline? |
| `core/AGENTS.md` | Rules complete? |
| `core/scraper/AGENTS.md` | DedupEngine lock rules documented? |
| `core/browser/AGENTS.md` | Stealth stack description accurate after changes? |
| `core/web_agent/AGENTS.md` | Controller rules complete? |
| `pipelines/scraper/AGENTS.md` | Rules complete? |
| `docs/architecture.md` | Reflects current package boundaries? |
| `docs/decisions.md` | D15–D18 present and accurate? |
| `docs/glossary.md` | New terms defined? |
| `pyproject.toml` | Dependencies pinned? Test config correct? |

---

## Audit Dimensions

### Dimension 1: Remediation Integrity

Verify every item in the Remediation Matrix above (R1–R11). For each:
- Read the actual code at the cited location
- Confirm the fix is correct and complete
- Check whether the fix introduced any new issues (regressions, type errors, dead code)
- Verify test coverage for the fixed behavior
- Assign PASS / PARTIAL / FAIL with justification

### Dimension 2: Security Red Team

Think like an attacker. For each attack surface:

**Trigger/Auth Attacks:**
- Can you forge a heal trigger token? What if you know the heal_id but not the secret?
- Can you replay a valid token after TTL expiry by manipulating the `issued_at` field?
- What happens if `KP_HEAL_TRIGGER_SIGNING_SECRET` is a weak secret (e.g., "password")?
- Is `hmac.compare_digest` actually used (timing-safe comparison)?

**Path/Storage Attacks:**
- Can a malicious `site_id` in `DedupEngine`, `ContentStore`, or `FixtureStore` escape the base directory?
- What if `site_id` contains `../`, null bytes, or shell metacharacters?
- Are there any `os.path.join` calls that don't go through `safe_join`?

**Data Leakage:**
- Search ALL structlog calls for potential secret leakage (passwords, tokens, API keys)
- Check all error messages and exception handlers for credential exposure
- Verify LLM prompts never contain raw credentials (check `context.py`, `onboard.py`, `heal.py`)
- Can `repr()` or `str()` on any Pydantic model expose `SecretStr` values?

**Injection:**
- The `DedupEngine` uses `f"[{table}]"` for table names. Can `site_id` inject SQL?
- Can a malicious email body in `inbox.py` trigger unintended behavior beyond the `fix` keyword?
- Can LLM output in `onboard.py` or `heal.py` escape its intended scope (prompt injection)?

**Dependency/Runtime:**
- Are `mmh3`, `aioimaplib`, `httpx` at safe versions? Any known CVEs?
- Does `asyncio.to_thread` for SQLite operations correctly handle connection thread safety?
- What happens if the SQLite database file is corrupted?

### Dimension 3: Stealth / Anti-Detection Adversarial Review

**You are now a senior anti-bot engineer at Cloudflare/DataDome.** Evaluate each stealth measure:

**Fingerprint Coherence (most important):**
- After the WebGL platform fix: if `apply_stealth_defaults()` picks a macOS User-Agent, does the init script detect "Macintosh" in `navigator.userAgent` and select the correct WebGL strings? Trace the full flow from UA selection → context creation → init script execution.
- Are `Sec-CH-UA` hints consistent with the spoofed User-Agent?
- Does the font list in the font enumeration defense match the platform? (The current list includes "Comic Sans MS" — is that available on Linux?)
- Are timezone and locale consistent with the geolocation implied by the UA/IP?

**Detectable Anomalies:**
- Canvas noise: if a page calls `toDataURL()` twice on the same canvas, does it get different results? Real browsers return identical results. Variable noise per call is a detection signal.
- AudioContext: the oscillator frequency offset is applied at creation time. If a detection script creates many oscillators, can it detect that all of them have the same small offset pattern?
- `navigator.webdriver`: after the fix to `false`, can detection scripts still detect automation via other Playwright artifacts? (e.g., `window.__playwright`, `navigator.languages` being a non-standard value, `window.cdc_adoQpoasnfa76pfcZLmcfl_*` properties from ChromeDriver)
- WebRTC: does `iceServers: []` cause `RTCPeerConnection` to fail in a way that's distinguishable from a browser with no STUN servers configured?
- Permissions API: returning `{state: 'prompt'}` for notifications — is this consistent with the actual Notification.permission value?

**HTTP Client Stealth:**
- `httpx` has a distinctive TLS fingerprint (JA3/JA4). Modern bot detection correlates TLS fingerprint with User-Agent. Is this addressed?
- After the `Sec-Fetch-Site: none` fix: are other `Sec-Fetch-*` headers consistent? (`Sec-Fetch-Mode: navigate`, `Sec-Fetch-Dest: document`, `Sec-Fetch-User: ?1` — is `?1` correct for all request types?)
- Is `Accept-Encoding` consistent with what the HTTP client actually supports?
- Header ordering: do browsers send headers in a specific order? Does `httpx` match?
- Cookie handling: does the HTTP client maintain cookies between requests to simulate a session?

**Rate Limiting & Behavioral Realism:**
- Are the default delays in site profiles realistic for human behavior? Too fast? Too uniform?
- Does the rate limiter add jitter to prevent periodic patterns?
- Is there anti-fingerprinting in the timing of requests (e.g., don't always request at exact intervals)?

### Dimension 4: Correctness and Robustness

**Dedup Engine:**
- Is the Bloom filter false-positive rate calculation correct? (`-n * ln(p) / (ln(2)^2)` for bits, `bits / n * ln(2)` for hash count)
- After the `_add_batch_locked` fix: does the `SELECT COUNT(*) ... WHERE key IN (...)` UPDATE correctly handle the case where a key appears multiple times in the same batch?
- What happens if `prune()` is called concurrently with `add_batch()` from different async tasks?
- Is `rebuild_bloom` correct? Does it iterate all keys for the site and add them to a fresh bloom?

**Content Store:**
- Is JSONL append atomic on the filesystem? (It isn't on all filesystems for large writes.)
- What happens if `compress_old()` runs while a writer is appending to today's file?
- Are there any edge cases in date partitioning (timezone boundaries, DST transitions)?

**Fixture System:**
- Does `compute_fingerprint()` capture meaningful structural changes? Could a complete DOM rewrite produce a similar fingerprint if it happens to have the same tag counts?
- Is the 0.85 similarity threshold well-calibrated? What evidence supports this choice?
- Can golden records grow unboundedly?

**Web Agent:**
- Can the observe-act loop get stuck in an infinite cycle (e.g., clicking the same element repeatedly)?
- What happens if the LLM consistently returns low-confidence actions?
- Is there a timeout on individual action execution (e.g., a `fill` that hangs)?

**Wrappers:**
- LinkedIn infinite scroll: is there a hard limit to prevent scrolling forever?
- ASP.NET postback: what happens when `__VIEWSTATE` is encrypted/compressed (as in newer .NET versions)?
- US Land Records vendor detection: can misidentification cause extraction to silently produce wrong data?
- Are all pagination paths guarded against infinite loops?

### Dimension 5: Performance and Scalability

- **Dedup `to_thread`**: Every `add_batch` and `prune` wraps sync SQLite in `asyncio.to_thread`. At high call frequency, does the thread pool become a bottleneck?
- **Bloom filter memory**: What's the memory footprint at 1M keys with `bloom_expected=1_000_000`?
- **Lock contention**: The dedup engine uses a single `asyncio.Lock` for all sites. Could per-site locks improve throughput?
- **`SELECT COUNT(*)` overhead**: The new counting approach runs two `COUNT(*)` queries per batch. At 500K+ rows, is this fast enough? (Should be — SQLite `COUNT(*)` on a table without WHERE is O(1) via internal counter, but `COUNT(*)` after a batch before commit may not use this optimization.)
- **Content store fsync**: Does each `append()` call fsync? What's the write amplification?
- **Stealth script overhead**: The init script runs before every page load. Is there measurable latency? Could it delay page interaction and be detectable via timing?
- **CI test suite**: 437 tests in 78 seconds. Is the 100K dedup scale test a bottleneck? Could it slow CI as the suite grows?

### Dimension 6: Architecture and Modularity

- **`core/` isolation**: Grep every file in `core/` for imports from `pipelines/`. Any violation is a bug.
- **Pipeline self-containment**: Do any `pipelines/scraper/` files import from `pipelines/job_agent/`?
- **Duplication between pipelines**: Are there similar patterns in `pipelines/job_agent/discovery/` and `pipelines/scraper/` that should share code via `core/`? Or is the current separation correct?
- **Wrapper registry scaling**: `resolve_wrapper` uses prefix matching. With 50+ wrappers, is this O(n) scan acceptable? Could it produce ambiguous matches?
- **SiteProfile expressiveness**: Can the YAML schema handle sites that require: OAuth flows? CAPTCHA-gated pages? Multi-step authentication? Dynamic API endpoints?
- **Heal contract**: Can the healing mechanism handle more than selector drift? Auth flow changes? New CAPTCHA types? API deprecation?
- **`ScrapeReport` model**: Is it used anywhere? If not, is it dead code?

### Dimension 7: Tests and Quality of Tests

For every test file, evaluate:
- **Assertions**: Are they testing behavior or implementation details?
- **Negative paths**: Does each feature have tests for rejection/failure cases?
- **Brittleness**: Are tests coupled to internal implementation (e.g., checking exact log messages, private method calls)?
- **Missing edge cases**: What inputs/states could break the code but aren't tested?
- **False confidence**: Are there tests that always pass regardless of code correctness? (e.g., tests that mock so aggressively they test the mock, not the code)

**Specific test gaps to investigate:**
- Is there a test that verifies `prune()` and `add_batch()` are safe to call concurrently?
- Is there a test that verifies `safe_join` rejects symlink escape attempts?
- Is there a test that the heal node's `was_already_attempted()` actually prevents re-execution?
- Are there integration tests for the full scrape→validate→heal flow?
- Is there a test for `compress_old()` in ContentStore?
- Do LinkedIn/Indeed wrapper tests verify that authentication uses BrowserManager?

### Dimension 8: Documentation and Operational Readiness

- **`.env.example`**: Every `KP_*` setting has a description? Sensitive defaults (like empty signing secrets) are flagged?
- **AGENTS.md files**: Every directory with an `AGENTS.md` — is it accurate after the recent changes?
- **`docs/architecture.md`**: Does it show the current package layout, including `core/scraper/`, `core/web_agent/`, `core/notifications/`?
- **`docs/decisions.md`**: D15 (profile+wrapper architecture), D16 (shared scraper primitives), D17 (signed heal trigger), D18 (IMAP replay) — all present and accurate?
- **`docs/glossary.md`**: New terms defined: SiteProfile, OutputContract, structural fingerprint, golden record, remediation report, heal trigger token?
- **Operational monitoring**: How would an operator know the scraper is failing silently? Are there sufficient structlog events for alerting?
- **Graceful degradation**: What happens when LLM is unavailable? When IMAP is unreachable? When SQLite locks?
- **Data retention**: Dedup records, fixtures, content store files — is there a mechanism to clean up? Does `prune()` cover all storage, or just dedup?

---

## Required Output Format

Structure your output EXACTLY as follows. Do not skip sections. Do not abbreviate.

```
## Executive Summary
[2-3 paragraphs: overall quality, most critical findings, architectural health,
assessment of prior audit's remediation quality]

## Remediation Integrity Matrix

| ID | Verdict | Evidence |
|----|---------|----------|
| R1 | PASS/PARTIAL/FAIL | [cite specific lines, explain reasoning] |
| R2 | ... | ... |
| ... | ... | ... |
| R11 | ... | ... |

**Summary:** X/11 PASS, Y PARTIAL, Z FAIL

## Critical Issues (must fix before merge)
[Numbered list. Each item: file path, line numbers, description, severity justification,
concrete fix (actual code, not vague suggestion)]

## High-Priority Issues
[Same format]

## Medium-Priority Issues
[Same format]

## Low-Priority / Informational
[Same format]

## Stealth-Specific Findings
[Dedicated section. For each stealth measure in ANTI_DETECTION_SCRIPT and
http_client.py, state: (a) what it defends against, (b) effectiveness against
modern detection, (c) whether it introduces detectable artifacts, (d) recommended
improvement if any]

## Security Threat Scenarios
[At least 3 concrete attack scenarios with steps to reproduce and impact assessment]

## Architectural Recommendations
[Structural changes that would improve the system. Must be justified — not
bikeshedding. Each recommendation should reference a specific problem found
during the audit.]

## Missing / Weak Tests
[Specific test cases that should be added. For each, describe: what it tests,
why it matters, and a sketch of the test implementation]

## File-by-File Review Ledger
[For EVERY file in the mandatory review scope, one line:
"reviewed `<file>` (lines X–Y), <N issues found / no issues found>"]

## Summary Statistics
- Files reviewed: N
- Issues found: N (N critical, N high, N medium, N low/info)
- Remediation matrix: N/11 PASS, N PARTIAL, N FAIL
- Estimated effort to resolve all issues: [hours]
```

---

## Non-Negotiable Rules for Your Output

1. **Every finding must cite a specific file and line number.** "The dedup engine has a race condition" is unacceptable. "`core/scraper/dedup.py:287` — `rebuild_bloom` is called outside the lock" is acceptable.

2. **Every proposed fix must be concrete.** Show the actual code change, not "consider adding validation."

3. **Every file in the mandatory scope must appear in the File-by-File Review Ledger.** If you ran out of context or couldn't review a file, say so explicitly — do not silently skip it.

4. **Distinguish between "I verified this is correct" and "I didn't find anything wrong."** The former is a confident positive assertion. The latter may mean you didn't look hard enough.

5. **Be adversarial.** If a security measure looks correct, try to break it. If a test looks thorough, look for what it doesn't test. If a stealth measure looks effective, think about how an anti-bot engineer would detect it.

6. **Do not trust comments or docstrings.** Verify that the code does what the comments claim. The prior audit found a docstring claiming IDLE support where the code was a poll loop.

7. **If you find the prior audit's fix introduced a new bug, that is a CRITICAL finding.** Fixes that break other things are worse than the original issue.

8. **Run the validation gates yourself** (`ruff check`, `ruff format --check`, `mypy`, `pytest`) and include the results. If any fail, that is a CRITICAL finding.
