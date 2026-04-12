"""Filtering node — apply the salary floor and other hard gates.

Keeps listings that meet the posted salary floor, marks the rest
``FILTERED_OUT``. A separate setting governs the policy for listings
with no posted salary: by default they are passed through so no jobs
are silently lost. Set ``filter_allow_unknown_salary=False`` to drop
no-salary listings if you want to enforce a hard floor.
"""

from __future__ import annotations

import structlog

from core.config import get_settings
from pipelines.job_agent.models import ApplicationStatus, JobListing
from pipelines.job_agent.state import JobAgentState, PipelinePhase

logger = structlog.get_logger(__name__)


def _passes_salary_filter(listing: JobListing, floor: int, *, allow_unknown: bool) -> bool:
    """Check whether a listing clears the salary floor.

    Rules:
      - ``salary_min`` at or above the floor → pass.
      - ``salary_max`` at or above the floor → pass (range straddles it).
      - Both fields missing → pass only when ``allow_unknown`` is set.
      - Otherwise → fail.
    """
    if listing.salary_min is not None and listing.salary_min >= floor:
        return True
    if listing.salary_max is not None and listing.salary_max >= floor:
        return True
    if listing.salary_min is None and listing.salary_max is None:
        return allow_unknown
    return False


async def filtering_node(state: JobAgentState) -> JobAgentState:
    """Filter discovered listings against search criteria.

    Applies the configured salary floor and marks filtered-out listings
    with ``ApplicationStatus.FILTERED_OUT`` so the tracking node still
    records them (important for accurate dedup across runs).
    """
    state.phase = PipelinePhase.FILTERING
    salary_floor = state.search_criteria.salary_floor
    allow_unknown = get_settings().filter_allow_unknown_salary

    qualified: list[JobListing] = []
    filtered_unknown = 0
    filtered_below_floor = 0

    for listing in state.discovered_listings:
        if _passes_salary_filter(listing, salary_floor, allow_unknown=allow_unknown):
            qualified.append(listing)
            continue
        listing.status = ApplicationStatus.FILTERED_OUT
        if listing.salary_min is None and listing.salary_max is None:
            filtered_unknown += 1
        else:
            filtered_below_floor += 1

    state.qualified_listings = qualified
    logger.info(
        "filtering_complete",
        input_count=len(state.discovered_listings),
        qualified=len(qualified),
        filtered_below_floor=filtered_below_floor,
        filtered_unknown_salary=filtered_unknown,
        salary_floor=salary_floor,
        allow_unknown_salary=allow_unknown,
    )
    return state
