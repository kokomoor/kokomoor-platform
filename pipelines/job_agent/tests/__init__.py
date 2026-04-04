"""Shared test fixtures for the job agent pipeline."""

from __future__ import annotations

import pytest

from pipelines.job_agent.models import JobListing, JobSource, SearchCriteria
from pipelines.job_agent.state import JobAgentState


@pytest.fixture
def sample_search_criteria() -> SearchCriteria:
    """Default search criteria for testing."""
    return SearchCriteria(
        keywords=["technical product manager", "senior engineer"],
        target_companies=["Anduril", "NVIDIA", "CFS"],
        salary_floor=170_000,
        sources=[JobSource.WELLFOUND, JobSource.BUILTIN],
    )


@pytest.fixture
def sample_listing() -> JobListing:
    """A single sample job listing for testing."""
    return JobListing(
        title="Senior Technical Product Manager",
        company="Anduril Industries",
        location="Costa Mesa, CA",
        url="https://jobs.lever.co/anduril/abc123",
        source=JobSource.LEVER,
        description="Lead autonomous systems product development...",
        salary_min=180_000,
        salary_max=250_000,
        remote=False,
        dedup_key="test_dedup_key_anduril_tpm",
    )


@pytest.fixture
def initial_state(sample_search_criteria: SearchCriteria) -> JobAgentState:
    """A fresh pipeline state for testing."""
    return JobAgentState(
        search_criteria=sample_search_criteria,
        run_id="test-run-001",
    )
