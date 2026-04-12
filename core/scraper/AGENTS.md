# core/scraper/ — Shared Scraper Infrastructure

Domain-agnostic modules that any scraper pipeline can import.

| Module | Purpose |
|--------|---------|
| `dedup.py` | SQLite + Bloom filter dedup engine (100K+ scale, partitioned by site, TTL pruning) |
| `content_store.py` | Append-only JSONL persistence partitioned by site and date |
| `http_client.py` | Stealth HTTP client with header rotation, block detection, escalation signals, robots.txt |
| `fixtures.py` | Fixture capture, structural fingerprinting, drift comparison, golden record storage |

## Rules

- **No pipeline-specific logic here.** All models live in the pipeline.
- All public functions must be typed.
- Use `structlog.get_logger(__name__)` for all logging.
- DedupEngine is not thread-safe for writes — use the internal `asyncio.Lock`.
- Fingerprints compare structure, not content — text changes should not trigger drift.
