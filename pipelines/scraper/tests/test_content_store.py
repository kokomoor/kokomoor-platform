"""Tests for ContentStore durability behavior."""

from __future__ import annotations

import threading
from datetime import date, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from core.scraper.content_store import ContentStore


def test_append_and_read_round_trip(tmp_content_store: ContentStore) -> None:
    written = tmp_content_store.append("test_site", [{"a": 1}, {"a": 2}])
    assert written == 2
    records = tmp_content_store.read("test_site")
    assert len(records) == 2


def test_compress_old_files(tmp_content_store: ContentStore) -> None:
    tmp_content_store.append("test_site", [{"a": 1}])
    site_dir = tmp_content_store._site_dir("test_site")
    today = date.today().isoformat()
    path = site_dir / f"{today}.jsonl"
    old_day = (date.today() - timedelta(days=10)).isoformat()
    old_path = site_dir / f"{old_day}.jsonl"
    path.replace(old_path)

    compressed = tmp_content_store.compress_old("test_site")
    assert compressed == 1
    assert (site_dir / f"{old_day}.jsonl.gz").exists()


def test_rejects_unsafe_site_id(tmp_content_store: ContentStore) -> None:
    import pytest

    with pytest.raises(ValueError):
        tmp_content_store.append("../escape", [{"a": 1}])


def test_compress_old_handles_missing_base_dir(tmp_path: Path) -> None:
    from core.scraper.content_store import ContentStore

    store = ContentStore(tmp_path / "does-not-exist")
    assert store.compress_old() == 0


def test_compress_old_is_safe_with_concurrent_append(tmp_content_store: ContentStore) -> None:
    site = "test_site"
    tmp_content_store.append(site, [{"seed": 1}])
    site_dir = tmp_content_store._site_dir(site)
    today = date.today().isoformat()
    old_day = (date.today() - timedelta(days=10)).isoformat()
    (site_dir / f"{today}.jsonl").replace(site_dir / f"{old_day}.jsonl")

    def writer() -> None:
        for i in range(20):
            tmp_content_store.append(site, [{"n": i}])

    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    compressed = tmp_content_store.compress_old(site)
    writer_thread.join()

    assert compressed == 1
    assert (site_dir / f"{old_day}.jsonl.gz").exists()
