# Pipeline Remediation — 2026-04-11

A focused round of fixes for the issues found during the previous credit-limited
audit. Twelve items in three buckets: critical bugs, architectural problems, and
quality problems. Each section names the file, what changed, and why the change
was necessary.

The full test suite (`pipelines/job_agent/tests` + `core/tests`) is green after
these changes — 347 passing.

---

## Critical bugs

### C1 — Ranking by salary instead of fit
**File:** `pipelines/job_agent/nodes/ranking.py` (rewritten)

The previous ranker sorted listings by `salary_max` then `salary_min`, with no
fit signal at all. Salary fields are unreliable (most providers post no number,
many post hourly or token-as-comp ranges, and a few post the wrong field
entirely), so the top-N pick was almost random — and any listing that *did*
publish a salary always sorted above better-fitting listings that didn't.

The new ranker scores each listing on three signals:

```
fit = 0.55 * basic_coverage + 0.25 * preferred_coverage + 0.20 * keyword_coverage
```

Each `*_coverage` is the fraction of requirement phrases (basic quals,
preferred quals, must-hit keywords from the job analysis) whose substantive
tokens appear in the candidate corpus (skills + bullet text). Stopwords and
sub-3-character tokens are dropped; an empty/boilerplate phrase contributes
nothing rather than dragging the score down.

After scoring, the ranker drops everything below `ranking_min_fit_score` (new
config knob, default 0.35) and selects the top `tailoring_max_listings`. Salary
is now a tiebreaker only.

This is intentionally a *deterministic* fit signal — no extra LLM calls, no
new training data. A future LLM-graded ranker can layer on top, but the
current change closes the bug without paying token cost.

### C2 — Unreliable salary as the sort key
**File:** `pipelines/job_agent/nodes/ranking.py`, `core/config.py`

Same root cause as C1, fixed by the same rewrite. The new `tailoring_max_listings`
default of 5 (was 0 / unlimited) caps blast radius if a future regression
re-introduces a noisy ranking signal — at most 5 wasteful Sonnet cover-letter
calls instead of dozens.

---

## Architectural problems

### A1 / A3 — Pipeline order with prefilter disabled
**File:** `core/config.py`

The discovery prefilter was off-by-default (`discovery_prefilter_min_score=0.0`),
which meant every raw discovery ref — including listings the keyword/role
matcher could clearly reject — flowed into bulk extraction and job analysis
unfiltered. With LinkedIn returning 124+ refs per run, this was the single
biggest source of wasted Haiku calls.

Default raised to **0.25**, which keeps any listing with a role match, a
keyword hit, a target-company hit, or a location hit (any single signal worth
≥0.25 in the rule-based scorer), and rejects refs with literally no match at
all. The bypass branch that fires when `min_score <= 0.0` is preserved for
exploratory runs.

### A2 — Sequential bulk_extraction with global sleep
**File:** `pipelines/job_agent/nodes/bulk_extraction.py` (rewritten)

The previous loop fetched listings strictly sequentially with a 1.5–4 second
sleep between each one. Thirty listings turned into ~2 minutes of wall time
for what is essentially I/O-bound work.

Now uses `asyncio.gather` under a semaphore sized by
`settings.llm_max_concurrency` (which doubles as the I/O concurrency knob — a
dedicated setting felt premature for current pipeline volume). The per-task
jitter sits *inside* the semaphore window so we keep concurrency while still
avoiding synchronised requests at the same origin. A failure on one listing
no longer cancels the others; it marks the listing `ERRORED` and continues.

### A4 — No process-wide LLM throttle (RateLimitError stalls)
**File:** `core/llm/throttle.py` (new), `core/llm/anthropic.py`, `core/config.py`

The job-analysis fan-out was bursting past the org's per-minute output-token
ceiling (10K/min default), tripping a `RateLimitError`, and then sitting at
65 seconds of `_adaptive_wait` *per offended call*. The pipeline wasn't broken
— it was working as designed and waiting for the quota window to reset — but
the burst pattern guaranteed it would hit the limit on every Haiku-heavy run.

`core/llm/throttle.py` is a new module with two primitives:

- **`global_semaphore`** — a process-wide cap on concurrent in-flight
  `messages.create` calls. The per-engine `llm_max_concurrency` only caps a
  single node's fan-out; if cover-letter and resume tailoring ever ran in
  parallel they could double up. A process-wide cap closes that hole.
- **`TokenBucket`** — a rolling 60-second output-token budget. Every call
  reserves its `max_tokens` budget up front; when the in-window total would
  exceed `llm_output_tokens_per_minute` (default 10,000), the call awaits
  until the oldest reservation expires. We hit the limit pre-emptively and
  pace the burst, rather than firing it and eating a 65-second 429.

Both primitives are created lazily inside `get_throttle()` so test
environments that never touch the LLM don't pay a cost for them. The
`AnthropicClient.complete()` path now wraps every API call in
`async with throttle.global_semaphore: await throttle.output_token_bucket.acquire(max_tokens)`.

### A5 — Master profile loaded N times per run
**File:** `pipelines/job_agent/resume/profile.py`

`load_master_profile()` was called once per node from ranking, tailoring, and
cover-letter tailoring — the YAML was parsed and Pydantic-validated three
times in a typical run, and on a per-listing basis in the test fixtures.

Now keyed by `(resolved_path, mtime_ns)` in a module-level cache. Same path
+ same mtime → return the cached `ResumeMasterProfile`. A manual edit on
disk picks up cleanly without an explicit `cache_clear()`, which is what
the tests need.

### A6 — Greenhouse / Lever providers silently disabled
**File:** `core/config.py`

The provider enable flags defaulted to `True`, but the orchestrator
short-circuits on `config.greenhouse_companies` / `config.lever_companies`
being empty — and the company list defaults were `""`. So both providers
*looked* enabled in the audit but contributed zero refs to every run.

Default seed lists added: 23 well-known Greenhouse boards (Anduril, Scale,
Databricks, Stripe, etc.) and 18 Lever boards (Anthropic, Palantir, Cohere,
etc.). These are companies the user is likely to want regardless of search
criteria; users with a different focus can override via env vars (which
takes precedence over the default).

---

## Quality problems

### Q1 — Cover letter word budget was a soft warning
**File:** `pipelines/job_agent/cover_letter/validation.py`

Letters over the 420-word ceiling generated a warning and were still written
to disk. Recruiters skim, the cap is a hard requirement, and the
structured-output retry loop in `core/llm/structured.py` is exactly the
right place to handle "regenerate trimmed" — promote to `ValueError`. The
under-minimum case stays as a warning since some compressed openings are
intentional and salvageable.

### Q2 — Missing company name in body was a soft warning
**File:** `pipelines/job_agent/cover_letter/validation.py`

Same shape as Q1: the model has the company name in the prompt; refusing to
use it produces a letter indistinguishable from a generic template. Promoted
to `ValueError`. The validation retry loop will ask the model to fix it,
which is much cheaper than shipping a bad letter to a human recipient.

### Q3 — Prompt caching reported `cache_read_tokens=0`
**File:** `core/llm/structured.py`, `pipelines/job_agent/nodes/job_analysis.py`,
`pipelines/job_agent/nodes/tailoring.py`

Two distinct problems:

1. **Cover letter system prompt was below the cache minimum.** The system
   text + style guide came in around ~500 tokens, well under the 1024-token
   minimum for Sonnet/Opus prompt caching. Anthropic silently doesn't write
   the cache below that floor — hence the 0 reads.
2. **`job_analysis` and `tailoring` had no cached system prefix at all.**
   Both nodes passed the entire static template inline in the user prompt,
   so every Haiku/Sonnet call paid full prefill cost.

Fixes:

- `structured_complete` now folds the JSON schema into the system message
  whenever `cache_system=True`. The schema is identical across calls, so it
  belongs in the cacheable prefix; this also pushes the prefix above the
  per-model minimum even when the caller's own `system_prefix` is small.
  When `cache_system=False`, the legacy behaviour (schema appended to user
  prompt) is preserved.
- `job_analysis` now defines `_ANALYSIS_SYSTEM` (the static instructions
  block) and passes it via `build_cached_system`. With the schema folded
  in, this comfortably clears Haiku's 2048-token cache minimum.
- `tailoring` does the same with `_RESUME_PLAN_SYSTEM` (the static rules
  block + page-fill constraints).

### Q4 — No per-source visibility into the discovery funnel
**File:** `pipelines/job_agent/nodes/discovery.py`

Discovery logged `total_refs` after dedup and the prefilter pass, but
nothing per-source. So when LinkedIn returned 124 refs and only 30 made it
to bulk extraction, you couldn't tell whether dedup ate them, the prefilter
ate them, or the provider was just over-counting.

Now emits a `discovery.source_funnel` event per source with five numbers:
`raw → deduped → prefilter_dropped → kept`. The aggregate
`discovery.prefilter_results` event is preserved for backwards-compatible
log parsing.

---

## Verification

```
$ python -m pytest pipelines/job_agent/tests/ core/tests/ -q
=========================== 347 passed in ~90s ============================
```

Three test updates were needed:

- `test_validation_warns_on_missing_company_in_body` renamed to
  `test_validation_rejects_missing_company_in_body` and asserts
  `pytest.raises(ValueError)` instead of inspecting the warnings list.
- Three `TestBulkExtractionNode` tests no longer wholesale-patch
  `bulk_extraction.asyncio` (which broke `asyncio.gather` / `Semaphore`).
  They now patch `asyncio.sleep` specifically.

## Files touched

- `core/config.py` — three new defaults: `tailoring_max_listings=5`,
  `ranking_min_fit_score=0.35`, `discovery_prefilter_min_score=0.25`,
  `llm_output_tokens_per_minute=10_000`; non-empty defaults for
  `greenhouse_target_companies` and `lever_target_companies`.
- `core/llm/throttle.py` — **new file**: process-wide semaphore + rolling
  token bucket.
- `core/llm/anthropic.py` — `complete()` now acquires the throttle before
  every API call.
- `core/llm/structured.py` — schema folded into the cached system message
  when `cache_system=True`.
- `pipelines/job_agent/nodes/ranking.py` — rewritten as a fit-score ranker.
- `pipelines/job_agent/nodes/bulk_extraction.py` — parallelised under
  semaphore.
- `pipelines/job_agent/nodes/discovery.py` — per-source funnel logging.
- `pipelines/job_agent/nodes/job_analysis.py` — added `_ANALYSIS_SYSTEM`
  cached prefix.
- `pipelines/job_agent/nodes/tailoring.py` — added `_RESUME_PLAN_SYSTEM`
  cached prefix.
- `pipelines/job_agent/cover_letter/validation.py` — promoted word-budget
  and company-in-body checks to hard failures.
- `pipelines/job_agent/resume/profile.py` — `(path, mtime)`-keyed cache.
- `pipelines/job_agent/tests/test_cover_letter_tailoring.py` — updated
  company-in-body test expectation.
- `pipelines/job_agent/tests/test_discovery_node.py` — three bulk-extraction
  tests updated to patch `asyncio.sleep` not `asyncio`.
