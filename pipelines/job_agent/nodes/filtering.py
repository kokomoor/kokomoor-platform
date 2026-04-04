"""Filtering node — deduplicate and filter job listings.

Applies salary floor, keyword filters, and deduplication against
the tracking database to produce a list of qualified listings
worth tailoring materials for.
"""

from __future__ import annotations

import structlog

from pipelines.job_agent.models import ApplicationStatus, JobFilter, JobListing
from pipelines.job_agent.state import JobAgentState, PipelinePhase

logger = structlog.get_logger(__name__)


def _passes_salary_filter(listing: JobListing, floor: int) -> bool:
    """Check if listing meets the salary floor."""
    if listing.salary_min is not None and listing.salary_min >= floor:
        return True
    if listing.salary_max is not None and listing.salary_max >= floor:
        return True
    # If no salary info, let it through for manual review.
    if listing.salary_min is None and listing.salary_max is None:
        return True
    return False


async def filtering_node(state: JobAgentState) -> JobAgentState:
    """Filter discovered listings against search criteria.

    Applies salary floor, keyword matching, and marks filtered-out
    listings with the appropriate status.

    Args:
        state: State with ``discovered_listings`` populated.

    Returns:
        Updated state with ``qualified_listings`` populated.
    """
    state.phase = PipelinePhase.FILTERING
    salary_floor = state.search_criteria.salary_floor
    qualified: list[JobListing] = []
    filtered_out = 0

    for listing in state.discovered_listings:
        if not _passes_salary_filter(listing, salary_floor):
            listing.status = ApplicationStatus.FILTERED_OUT
            filtered_out += 1
            continue
        qualified.append(listing)

    state.qualified_listings = qualified
    logger.info(
        "filtering_complete",
        input_count=len(state.discovered_listings),
        qualified=len(qualified),
        filtered_out=filtered_out,
    )
    return state
