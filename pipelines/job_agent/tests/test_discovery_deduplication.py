"""Tests for discovery deduplication with in-run and DB phases."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pipelines.job_agent.discovery.deduplication import compute_dedup_key, deduplicate_refs
from pipelines.job_agent.discovery.models import ListingRef
from pipelines.job_agent.models import JobSource


def _ref(
    url: str = "https://example.com/1",
    title: str = "SWE",
    company: str = "Acme",
) -> ListingRef:
    return ListingRef(url=url, title=title, company=company, source=JobSource.LINKEDIN)


class TestInRunDedup:
    @pytest.mark.asyncio
    async def test_duplicate_dropped(self) -> None:
        ref = _ref()
        seen: set[str] = set()
        result = await deduplicate_refs([ref, ref], in_run_seen=seen, check_db=False)
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_empty_input_returns_empty(self) -> None:
        result = await deduplicate_refs([], in_run_seen=set(), check_db=False)
        assert result == []

    @pytest.mark.asyncio
    async def test_all_unique_pass(self) -> None:
        refs = [_ref(url=f"https://example.com/{i}") for i in range(5)]
        result = await deduplicate_refs(refs, in_run_seen=set(), check_db=False)
        assert len(result) == 5

    @pytest.mark.asyncio
    async def test_check_db_false_skips_db(self) -> None:
        """When check_db=False, the DB import never runs so no DB access occurs."""
        ref = _ref()
        seen: set[str] = set()
        result = await deduplicate_refs([ref], in_run_seen=seen, check_db=False)
        assert len(result) == 1
        assert len(seen) == 1


class TestDbDedup:
    @pytest.mark.asyncio
    async def test_existing_key_excluded(self) -> None:
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

        with patch("core.database.get_session", return_value=mock_session):
            result = await deduplicate_refs([ref], in_run_seen=set(), check_db=True)

        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_non_existing_key_passes(self) -> None:
        ref = _ref()

        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars

        mock_session = AsyncMock()
        mock_session.execute.return_value = mock_result
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("core.database.get_session", return_value=mock_session):
            result = await deduplicate_refs([ref], in_run_seen=set(), check_db=True)

        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_db_failure_falls_back_to_in_run_only(self) -> None:
        refs = [_ref(url="https://example.com/1"), _ref(url="https://example.com/1")]
        seen: set[str] = set()

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(side_effect=RuntimeError("db unavailable"))
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("core.database.get_session", return_value=mock_session):
            result = await deduplicate_refs(refs, in_run_seen=seen, check_db=True)

        # In-run dedup still applies; DB failure should not crash or drop all data.
        assert len(result) == 1
