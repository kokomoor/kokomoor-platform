"""Content-addressed deduplication engine.

Scales to 100K+ records per site using a two-tier architecture:

1. **Bloom filter** — in-memory probabilistic check (~1 % false-positive rate).
   Negative results are authoritative (definitely new), positives are checked
   against SQLite for certainty.
2. **SQLite** — authoritative ground truth, one table per site partition.
   Supports batch upsert, TTL pruning, and cross-run persistence.

Typical hot-path cost: one ``mmh3`` hash + one bit-array lookup.
SQLite is only hit on Bloom positives (~1 % of new records) or on
explicit ``add_batch`` / ``prune`` calls.

Usage::

    dedup = DedupEngine(db_path="data/scraper_dedup.db")
    new_keys = await dedup.filter_new("linkedin", candidate_keys)
    await dedup.add_batch("linkedin", new_keys)
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib
import math
import sqlite3
from pathlib import Path
from typing import Any

import structlog

from core.scraper.path_safety import validate_site_id

# Optional C-extension for fast hashing; falls back to hashlib when absent.
# Loaded via importlib to avoid a module-level redefinition that strict mypy
# would flag as [no-redef] when the package is installed.
mmh3: Any = None
with contextlib.suppress(ImportError):
    mmh3 = importlib.import_module("mmh3")

logger = structlog.get_logger(__name__)

_DEFAULT_DB_PATH = Path("data/scraper_dedup.db")
_DEFAULT_TTL_DAYS = 90


# ---------------------------------------------------------------------------
# Bloom filter
# ---------------------------------------------------------------------------


class BloomFilter:
    """Fixed-size Bloom filter backed by a ``bytearray``.

    Designed for ~100K-1M items with a <=1 % false-positive rate.
    """

    def __init__(self, expected_items: int = 500_000, fp_rate: float = 0.01) -> None:
        if expected_items < 1:
            expected_items = 1
        self._num_bits = self._optimal_size(expected_items, fp_rate)
        self._num_hashes = self._optimal_hashes(self._num_bits, expected_items)
        self._bits = bytearray(math.ceil(self._num_bits / 8))
        self._count = 0

    @staticmethod
    def _optimal_size(n: int, p: float) -> int:
        return max(64, int(-n * math.log(p) / (math.log(2) ** 2)))

    @staticmethod
    def _optimal_hashes(m: int, n: int) -> int:
        return max(1, int((m / max(n, 1)) * math.log(2)))

    def _hash_indices(self, key: str) -> list[int]:
        if mmh3 is not None:
            h1 = mmh3.hash(key, seed=0, signed=False)
            h2 = mmh3.hash(key, seed=42, signed=False)
        else:
            raw = key.encode()
            h1 = int(hashlib.md5(raw).hexdigest()[:8], 16)
            h2 = int(hashlib.md5(raw + b"\x01").hexdigest()[:8], 16)
        return [(h1 + i * h2) % self._num_bits for i in range(self._num_hashes)]

    def add(self, key: str) -> None:
        for idx in self._hash_indices(key):
            self._bits[idx >> 3] |= 1 << (idx & 7)
        self._count += 1

    def might_contain(self, key: str) -> bool:
        return all(self._bits[idx >> 3] & (1 << (idx & 7)) for idx in self._hash_indices(key))

    @property
    def count(self) -> int:
        return self._count

    def clear(self) -> None:
        self._bits = bytearray(len(self._bits))
        self._count = 0


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------


def _table_name(site_id: str) -> str:
    """Sanitize site_id into a safe SQLite table name."""
    safe = validate_site_id(site_id)
    return f"dedup_{safe}"


def _ensure_table(conn: sqlite3.Connection, table: str) -> None:
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS [{table}] ("
        "  key TEXT PRIMARY KEY,"
        "  first_seen_ts REAL NOT NULL,"
        "  last_seen_ts REAL NOT NULL"
        ")"
    )
    conn.execute(f"CREATE INDEX IF NOT EXISTS [idx_{table}_last_seen] ON [{table}](last_seen_ts)")


# ---------------------------------------------------------------------------
# Dedup engine
# ---------------------------------------------------------------------------


class DedupEngine:
    """Two-tier dedup: Bloom filter + SQLite.

    Thread-safe for reads (Bloom is in-memory); writes serialize through
    an ``asyncio.Lock`` so concurrent ``add_batch`` calls don't corrupt
    the SQLite connection.
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        ttl_days: int = _DEFAULT_TTL_DAYS,
        bloom_expected: int = 500_000,
        bloom_fp_rate: float = 0.01,
    ) -> None:
        self._db_path = Path(db_path or _DEFAULT_DB_PATH)
        self._ttl_days = ttl_days
        self._bloom_expected = bloom_expected
        self._bloom_fp_rate = bloom_fp_rate
        self._blooms: dict[str, BloomFilter] = {}
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()
        self._initialized_tables: set[str] = set()

    # -- lifecycle -----------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        return self._conn

    def _ensure_site(self, site_id: str) -> str:
        table = _table_name(site_id)
        if table not in self._initialized_tables:
            _ensure_table(self._get_conn(), table)
            self._initialized_tables.add(table)
        return table

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # -- bloom management ----------------------------------------------------

    def _get_bloom(self, site_id: str) -> BloomFilter:
        if site_id not in self._blooms:
            bloom = BloomFilter(self._bloom_expected, self._bloom_fp_rate)
            table = self._ensure_site(site_id)
            conn = self._get_conn()
            cursor = conn.execute(f"SELECT key FROM [{table}]")
            loaded = 0
            for (key,) in cursor:
                bloom.add(key)
                loaded += 1
            self._blooms[site_id] = bloom
            logger.info("dedup.bloom_loaded", site_id=site_id, keys=loaded)
        return self._blooms[site_id]

    def rebuild_bloom(self, site_id: str) -> int:
        """Force-rebuild the Bloom filter from SQLite. Returns key count."""
        if site_id in self._blooms:
            del self._blooms[site_id]
        bloom = self._get_bloom(site_id)
        return bloom.count

    # -- public API ----------------------------------------------------------

    async def contains(self, site_id: str, key: str) -> bool:
        """Check if *key* exists for *site_id*. Fast Bloom → SQLite fallback."""
        async with self._lock:
            return await asyncio.to_thread(self._contains_locked, site_id, key)

    def _contains_locked(self, site_id: str, key: str) -> bool:
        bloom = self._get_bloom(site_id)
        if not bloom.might_contain(key):
            return False
        table = self._ensure_site(site_id)
        conn = self._get_conn()
        row = conn.execute(f"SELECT 1 FROM [{table}] WHERE key = ?", (key,)).fetchone()
        return row is not None

    async def contains_batch(self, site_id: str, keys: list[str]) -> set[str]:
        """Return the subset of *keys* that already exist for *site_id*."""
        async with self._lock:
            return await asyncio.to_thread(self._contains_batch_locked, site_id, keys)

    def _contains_batch_locked(self, site_id: str, keys: list[str]) -> set[str]:
        bloom = self._get_bloom(site_id)
        maybe_existing = [k for k in keys if bloom.might_contain(k)]
        if not maybe_existing:
            return set()

        table = self._ensure_site(site_id)
        conn = self._get_conn()
        existing: set[str] = set()
        chunk_size = 500
        for i in range(0, len(maybe_existing), chunk_size):
            chunk = maybe_existing[i : i + chunk_size]
            placeholders = ",".join("?" * len(chunk))
            cursor = conn.execute(
                f"SELECT key FROM [{table}] WHERE key IN ({placeholders})",
                chunk,
            )
            existing.update(row[0] for row in cursor)
        return existing

    async def filter_new(self, site_id: str, keys: list[str]) -> list[str]:
        """Return only the keys that are NOT yet stored for *site_id*."""
        existing = await self.contains_batch(site_id, keys)
        return [k for k in keys if k not in existing]

    async def try_claim(self, site_id: str, key: str) -> bool:
        """Atomic check-and-set to claim a key. Returns True if claimed, False if already exists."""
        async with self._lock:
            return await asyncio.to_thread(self._try_claim_locked, site_id, key)

    def _try_claim_locked(self, site_id: str, key: str) -> bool:
        import time
        now = time.time()
        table = self._ensure_site(site_id)
        conn = self._get_conn()
        bloom = self._get_bloom(site_id)
        
        if self._contains_locked(site_id, key):
            return False
            
        try:
            conn.execute(
                f"INSERT INTO [{table}](key, first_seen_ts, last_seen_ts) VALUES (?, ?, ?)",
                (key, now, now),
            )
            conn.commit()
            bloom.add(key)
            return True
        except sqlite3.IntegrityError:
            return False

    async def add_batch(self, site_id: str, keys: list[str]) -> int:
        """Insert new keys. Returns count of actually-new insertions."""
        if not keys:
            return 0
        async with self._lock:
            inserted = await asyncio.to_thread(self._add_batch_locked, site_id, keys)
            logger.info("dedup.batch_added", site_id=site_id, keys=len(keys), new=inserted)
            return inserted

    def _add_batch_locked(self, site_id: str, keys: list[str]) -> int:
        import time

        now = time.time()
        table = self._ensure_site(site_id)
        conn = self._get_conn()
        bloom = self._get_bloom(site_id)

        count_before: int = conn.execute(f"SELECT COUNT(*) FROM [{table}]").fetchone()[0]

        chunk_size = 500
        for i in range(0, len(keys), chunk_size):
            chunk = keys[i : i + chunk_size]
            conn.executemany(
                f"INSERT OR IGNORE INTO [{table}](key, first_seen_ts, last_seen_ts) "
                f"VALUES (?, ?, ?)",
                [(k, now, now) for k in chunk],
            )
            placeholders = ",".join("?" * len(chunk))
            conn.execute(
                f"UPDATE [{table}] SET last_seen_ts = ? "
                f"WHERE key IN ({placeholders}) AND first_seen_ts < ?",
                [now, *chunk, now],
            )
            for k in chunk:
                bloom.add(k)
        conn.commit()

        count_after: int = conn.execute(f"SELECT COUNT(*) FROM [{table}]").fetchone()[0]
        return count_after - count_before

    async def prune(self, site_id: str, *, max_age_days: int | None = None) -> int:
        """Remove keys older than *max_age_days*. Returns count removed."""
        import time

        ttl = max_age_days if max_age_days is not None else self._ttl_days
        cutoff = time.time() - (ttl * 86_400)

        async with self._lock:
            removed = await asyncio.to_thread(self._prune_locked, site_id, cutoff)
            if removed > 0:
                await asyncio.to_thread(self.rebuild_bloom, site_id)
                logger.info("dedup.pruned", site_id=site_id, removed=removed, ttl_days=ttl)
        return removed

    def _prune_locked(self, site_id: str, cutoff: float) -> int:
        table = self._ensure_site(site_id)
        conn = self._get_conn()
        cursor = conn.execute(
            f"DELETE FROM [{table}] WHERE last_seen_ts < ?",
            (cutoff,),
        )
        removed = cursor.rowcount
        conn.commit()
        return removed

    async def count(self, site_id: str) -> int:
        """Return total stored keys for *site_id*."""
        async with self._lock:
            return await asyncio.to_thread(self._count_locked, site_id)

    def _count_locked(self, site_id: str) -> int:
        table = self._ensure_site(site_id)
        conn = self._get_conn()
        row = conn.execute(f"SELECT COUNT(*) FROM [{table}]").fetchone()
        return int(row[0]) if row else 0

    async def stats(self, site_id: str) -> dict[str, int]:
        """Return diagnostic stats for *site_id*."""
        async with self._lock:
            bloom = await asyncio.to_thread(self._get_bloom, site_id)
            total = await asyncio.to_thread(self._count_locked, site_id)
        return {
            "sqlite_keys": total,
            "bloom_count": bloom.count,
            "bloom_bits": bloom._num_bits,
            "bloom_hashes": bloom._num_hashes,
        }


# ---------------------------------------------------------------------------
# Dedup key computation
# ---------------------------------------------------------------------------


def compute_dedup_key(fields: dict[str, str], dedup_field_names: list[str]) -> str:
    """Compute a content-addressed dedup key from canonical field values.

    Args:
        fields: The extracted record as a flat dict.
        dedup_field_names: Which field names compose the key (from OutputContract).

    Returns:
        A 32-char hex SHA-256 prefix.
    """
    parts = [str(fields.get(f, "")).lower().strip() for f in sorted(dedup_field_names)]
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]
