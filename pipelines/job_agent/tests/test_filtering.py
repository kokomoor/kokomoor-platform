"""Tests for the Filtering node."""

from __future__ import annotations

import pytest

from pipelines.job_agent.models import ApplicationStatus, JobListing, JobSource
from pipelines.job_agent.nodes.filtering import _passes_salary_filter, filtering_node
from pipelines.job_agent.state import JobAgentState, PipelinePhase, SearchCriteria


def _make_listing(salary_min: int | None = None, salary_max: int | None = None) -> JobListing:
    """Helper to create a listing with specific salary info."""
    return JobListing(
        title="Test Role",
        company="Test Co",
        url="https://example.com/job",
        source=JobSource.OTHER,
        salary_min=salary_min,
        salary_max=salary_max,
        dedup_key=f"test_{salary_min}_{salary_max}",
    )


class TestSalaryFilter:
    """Tests for salary filtering logic."""

    def test_above_floor(self) -> None:
        assert _passes_salary_filter(_make_listing(salary_min=200_000), 170_000) is True

    def test_below_floor(self) -> None:
        assert _passes_salary_filter(_make_listing(salary_min=100_000, salary_max=120_000), 170_000) is False

    def test_max_above_floor(self) -> None:
        """If max salary meets floor, listing passes."""
        assert _passes_salary_filter(_make_listing(salary_min=150_000, salary_max=200_000), 170_000) is True

    def test_no_salary_passes(self) -> None:
        """Listings without salary info are let through for manual review."""
        assert _passes_salary_filter(_make_listing(), 170_000) is True


class TestFilteringNode:
    """Tests for the filtering node."""

    @pytest.mark.asyncio
    async def test_filters_low_salary(self) -> None:
        """Listings below salary floor are filtered out."""
        state = JobAgentState(
            search_criteria=SearchCriteria(salary_floor=170_000),
            discovered_listings=[
                _make_listing(salary_min=200_000, salary_max=250_000),
                _make_listing(salary_min=80_000, salary_max=100_000),
            ],
        )
        result = await filtering_node(state)
        assert len(result.qualified_listings) == 1
        assert result.phase == PipelinePhase.FILTERING

    @pytest.mark.asyncio
    async def test_empty_input(self) -> None:
        """Empty discovered listings produces empty qualified listings."""
        state = JobAgentState(search_criteria=SearchCriteria())
        result = await filtering_node(state)
        assert result.qualified_listings == []
