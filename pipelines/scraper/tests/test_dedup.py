"""Tests for core.scraper.dedup — DedupEngine at scale.

Validates:
- Bloom filter correctness (no false negatives, bounded false positives)
- SQLite batch operations (add, contains, filter_new)
- TTL pruning
- Partitioning (multiple site IDs don't collide)
- Scale: 100K+ keys with acceptable memory and time
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from core.scraper.dedup import BloomFilter, DedupEngine, compute_dedup_key


class TestBloomFilter:
    """Unit tests for the Bloom filter in isolation."""

    def test_add_and_check(self) -> None:
        bf = BloomFilter(expected_items=1000, fp_rate=0.01)
        bf.add("key_1")
        bf.add("key_2")
        assert bf.might_contain("key_1")
        assert bf.might_contain("key_2")
        assert bf.count == 2

    def test_no_false_negatives(self) -> None:
        bf = BloomFilter(expected_items=10_000, fp_rate=0.01)
        keys = [f"item_{i}" for i in range(5000)]
        for k in keys:
            bf.add(k)
        for k in keys:
            assert bf.might_contain(k), f"False negative for {k}"

    def test_false_positive_rate_bounded(self) -> None:
        bf = BloomFilter(expected_items=10_000, fp_rate=0.01)
        for i in range(10_000):
            bf.add(f"present_{i}")
        fp_count = sum(1 for i in range(50_000) if bf.might_contain(f"absent_{i}"))
        fp_rate = fp_count / 50_000
        assert fp_rate < 0.03, f"FP rate {fp_rate:.4f} exceeds 3% tolerance"

    def test_clear(self) -> None:
        bf = BloomFilter(expected_items=100)
        bf.add("x")
        bf.clear()
        assert bf.count == 0
        assert not bf.might_contain("x")


class TestDedupEngine:
    """Integration tests for DedupEngine with SQLite."""

    @pytest.mark.asyncio
    async def test_add_and_contains(self, tmp_dedup: DedupEngine) -> None:
        await tmp_dedup.add_batch("site_a", ["k1", "k2", "k3"])
        assert await tmp_dedup.contains("site_a", "k1")
        assert await tmp_dedup.contains("site_a", "k2")
        assert not await tmp_dedup.contains("site_a", "k99")

    @pytest.mark.asyncio
    async def test_contains_batch(self, tmp_dedup: DedupEngine) -> None:
        keys = [f"key_{i}" for i in range(100)]
        await tmp_dedup.add_batch("site_a", keys)
        check = keys[:50] + [f"new_{i}" for i in range(50)]
        existing = await tmp_dedup.contains_batch("site_a", check)
        assert len(existing) == 50
        assert all(f"key_{i}" in existing for i in range(50))

    @pytest.mark.asyncio
    async def test_filter_new(self, tmp_dedup: DedupEngine) -> None:
        await tmp_dedup.add_batch("site_a", ["old_1", "old_2"])
        candidates = ["old_1", "new_1", "old_2", "new_2"]
        new = await tmp_dedup.filter_new("site_a", candidates)
        assert set(new) == {"new_1", "new_2"}

    @pytest.mark.asyncio
    async def test_site_partitioning(self, tmp_dedup: DedupEngine) -> None:
        await tmp_dedup.add_batch("site_a", ["shared_key"])
        await tmp_dedup.add_batch("site_b", ["other_key"])
        assert await tmp_dedup.contains("site_a", "shared_key")
        assert not await tmp_dedup.contains("site_a", "other_key")
        assert await tmp_dedup.contains("site_b", "other_key")
        assert not await tmp_dedup.contains("site_b", "shared_key")

    @pytest.mark.asyncio
    async def test_prune_removes_old_keys(self, tmp_path: Path) -> None:
        engine = DedupEngine(tmp_path / "dedup.db", ttl_days=0)
        await engine.add_batch("site_a", ["old_key"])

        import sqlite3

        conn = sqlite3.connect(str(tmp_path / "dedup.db"))
        conn.execute("UPDATE [dedup_site_a] SET last_seen_ts = last_seen_ts - 100000")
        conn.commit()
        conn.close()

        removed = await engine.prune("site_a", max_age_days=0)
        assert removed == 1
        assert not await engine.contains("site_a", "old_key")
        engine.close()

    @pytest.mark.asyncio
    async def test_count(self, tmp_dedup: DedupEngine) -> None:
        await tmp_dedup.add_batch("site_a", [f"k_{i}" for i in range(42)])
        assert await tmp_dedup.count("site_a") == 42

    @pytest.mark.asyncio
    async def test_stats(self, tmp_dedup: DedupEngine) -> None:
        await tmp_dedup.add_batch("site_a", ["x", "y", "z"])
        stats = await tmp_dedup.stats("site_a")
        assert stats["sqlite_keys"] == 3
        assert stats["bloom_count"] >= 3

    @pytest.mark.asyncio
    async def test_idempotent_add(self, tmp_dedup: DedupEngine) -> None:
        await tmp_dedup.add_batch("site_a", ["k1", "k2"])
        await tmp_dedup.add_batch("site_a", ["k1", "k2", "k3"])
        assert await tmp_dedup.count("site_a") == 3

    @pytest.mark.asyncio
    async def test_add_batch_returns_exact_new_count_for_duplicate_inputs(
        self, tmp_dedup: DedupEngine
    ) -> None:
        inserted = await tmp_dedup.add_batch("site_a", ["k1", "k1", "k2"])
        assert inserted == 2
        inserted_again = await tmp_dedup.add_batch("site_a", ["k1", "k2", "k2"])
        assert inserted_again == 0

    @pytest.mark.asyncio
    async def test_prune_and_add_batch_can_run_concurrently(self, tmp_path: Path) -> None:
        engine = DedupEngine(tmp_path / "dedup.db", ttl_days=0)
        await engine.add_batch("site_a", [f"seed_{i}" for i in range(100)])
        await asyncio.gather(
            engine.prune("site_a", max_age_days=365),
            engine.add_batch("site_a", [f"new_{i}" for i in range(100)]),
        )
        count = await engine.count("site_a")
        assert count >= 200
        assert await engine.contains("site_a", "new_42")
        engine.close()


class TestDedupEngineScale:
    """Scale tests ensuring acceptable performance at 100K+ keys."""

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_100k_keys(self, tmp_path: Path) -> None:
        engine = DedupEngine(
            tmp_path / "scale.db",
            bloom_expected=200_000,
            bloom_fp_rate=0.01,
        )

        n = 100_000
        keys = [f"record_{i:08d}" for i in range(n)]

        t0 = time.monotonic()
        await engine.add_batch("scale_site", keys)
        add_time = time.monotonic() - t0
        assert add_time < 30.0, f"Adding 100K keys took {add_time:.1f}s (expected <30s)"

        t0 = time.monotonic()
        existing = await engine.contains_batch("scale_site", keys[:10_000])
        check_time = time.monotonic() - t0
        assert len(existing) == 10_000
        assert check_time < 5.0, f"Checking 10K keys took {check_time:.1f}s (expected <5s)"

        t0 = time.monotonic()
        new_keys = [f"fresh_{i:08d}" for i in range(10_000)]
        new = await engine.filter_new("scale_site", new_keys)
        filter_time = time.monotonic() - t0
        assert len(new) == 10_000
        assert filter_time < 5.0, f"Filtering 10K keys took {filter_time:.1f}s (expected <5s)"

        assert await engine.count("scale_site") == n
        engine.close()


class TestComputeDedupKey:
    def test_deterministic(self) -> None:
        fields = {"title": "Hello", "url": "https://example.com"}
        k1 = compute_dedup_key(fields, ["title", "url"])
        k2 = compute_dedup_key(fields, ["title", "url"])
        assert k1 == k2

    def test_case_insensitive(self) -> None:
        k1 = compute_dedup_key({"title": "Hello", "url": "X"}, ["title", "url"])
        k2 = compute_dedup_key({"title": "hello", "url": "x"}, ["title", "url"])
        assert k1 == k2

    def test_field_order_independent(self) -> None:
        k1 = compute_dedup_key({"a": "1", "b": "2"}, ["a", "b"])
        k2 = compute_dedup_key({"a": "1", "b": "2"}, ["b", "a"])
        assert k1 == k2

    def test_different_values_differ(self) -> None:
        k1 = compute_dedup_key({"title": "A"}, ["title"])
        k2 = compute_dedup_key({"title": "B"}, ["title"])
        assert k1 != k2
