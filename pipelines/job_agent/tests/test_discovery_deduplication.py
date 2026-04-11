"""Tests for discovery deduplication with in-run, DB, and file fallback phases."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pipelines.job_agent.discovery.dedup_store import FileDedup
from pipelines.job_agent.discovery.deduplication import compute_dedup_key, deduplicate_refs
from pipelines.job_agent.discovery.models import ListingRef
from pipelines.job_agent.models import JobSource


def _ref(
    url: str = "https://example.com/1",
    title: str = "SWE",
    company: str = "Acme",
) -> ListingRef:
    return ListingRef(url=url, title=title, company=company, source=JobSource.LINKEDIN)


def _empty_file_dedup(tmp_path: Path | None = None) -> FileDedup:
    """Create a file dedup that doesn't interfere with other tests."""
    path = (tmp_path or Path("/tmp")) / "test_dedup.json"
    return FileDedup(path)


class TestInRunDedup:
    @pytest.mark.asyncio
    async def test_duplicate_dropped(self, tmp_path: Path) -> None:
        ref = _ref()
        seen: set[str] = set()
        with patch(
            "pipelines.job_agent.discovery.deduplication.FileDedup",
            return_value=_empty_file_dedup(tmp_path),
        ):
            result = await deduplicate_refs([ref, ref], in_run_seen=seen, check_db=False)
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_empty_input_returns_empty(self, tmp_path: Path) -> None:
        with patch(
            "pipelines.job_agent.discovery.deduplication.FileDedup",
            return_value=_empty_file_dedup(tmp_path),
        ):
            result = await deduplicate_refs([], in_run_seen=set(), check_db=False)
        assert result == []

    @pytest.mark.asyncio
    async def test_all_unique_pass(self, tmp_path: Path) -> None:
        refs = [_ref(url=f"https://example.com/{i}") for i in range(5)]
        with patch(
            "pipelines.job_agent.discovery.deduplication.FileDedup",
            return_value=_empty_file_dedup(tmp_path),
        ):
            result = await deduplicate_refs(refs, in_run_seen=set(), check_db=False)
        assert len(result) == 5

    @pytest.mark.asyncio
    async def test_check_db_false_skips_db(self, tmp_path: Path) -> None:
        ref = _ref()
        seen: set[str] = set()
        with patch(
            "pipelines.job_agent.discovery.deduplication.FileDedup",
            return_value=_empty_file_dedup(tmp_path),
        ):
            result = await deduplicate_refs([ref], in_run_seen=seen, check_db=False)
        assert len(result) == 1
        assert len(seen) == 1


class TestDbDedup:
    @pytest.mark.asyncio
    async def test_existing_key_excluded(self, tmp_path: Path) -> None:
        ref = _ref()
        existing_key = compute_dedup_key("Acme", "SWE", "https://example.com/1")

        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [existing_key]
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars

        mock_session = AsyncMock()
        mock_session.execute.return_value = mock_result
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "pipelines.job_agent.discovery.deduplication.get_session",
                return_value=mock_session,
            ),
            patch(
                "pipelines.job_agent.discovery.deduplication.FileDedup",
                return_value=_empty_file_dedup(tmp_path),
            ),
        ):
            result = await deduplicate_refs([ref], in_run_seen=set(), check_db=True)

        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_non_existing_key_passes(self, tmp_path: Path) -> None:
        ref = _ref()

        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars

        mock_session = AsyncMock()
        mock_session.execute.return_value = mock_result
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "pipelines.job_agent.discovery.deduplication.get_session",
                return_value=mock_session,
            ),
            patch(
                "pipelines.job_agent.discovery.deduplication.FileDedup",
                return_value=_empty_file_dedup(tmp_path),
            ),
        ):
            result = await deduplicate_refs([ref], in_run_seen=set(), check_db=True)

        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_db_failure_falls_back_to_file_dedup(self, tmp_path: Path) -> None:
        refs = [_ref(url="https://example.com/1"), _ref(url="https://example.com/1")]
        seen: set[str] = set()

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(side_effect=RuntimeError("db unavailable"))
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "pipelines.job_agent.discovery.deduplication.get_session",
                return_value=mock_session,
            ),
            patch(
                "pipelines.job_agent.discovery.deduplication.FileDedup",
                return_value=_empty_file_dedup(tmp_path),
            ),
        ):
            result = await deduplicate_refs(refs, in_run_seen=seen, check_db=True)

        assert len(result) == 1


class TestFileDedupStore:
    def test_add_and_contains(self, tmp_path: Path) -> None:
        store = FileDedup(tmp_path / "dedup.json")
        store.add("key-1")
        assert store.contains("key-1")
        assert not store.contains("key-2")

    def test_save_and_reload(self, tmp_path: Path) -> None:
        path = tmp_path / "dedup.json"
        store = FileDedup(path)
        store.add_batch(["a", "b", "c"])
        store.save()

        store2 = FileDedup(path)
        assert store2.contains("a")
        assert store2.contains("b")
        assert store2.contains("c")
        assert not store2.contains("d")
        assert store2.size == 3

    def test_batch_contains(self, tmp_path: Path) -> None:
        store = FileDedup(tmp_path / "dedup.json")
        store.add_batch(["x", "y"])
        result = store.contains_batch(["x", "y", "z"])
        assert result == {"x", "y"}

    def test_cross_run_dedup_integration(self, tmp_path: Path) -> None:
        """Simulate two runs -- second run should skip keys from the first."""
        path = tmp_path / "dedup.json"
        store1 = FileDedup(path)
        store1.add_batch(["key-a", "key-b"])
        store1.save()

        store2 = FileDedup(path)
        existing = store2.contains_batch(["key-a", "key-b", "key-c"])
        assert existing == {"key-a", "key-b"}
