"""Ranking node — select the top-N listings for LLM-intensive tailoring.

Sits between job_analysis (cheap, runs on all qualified listings) and
tailoring (expensive Sonnet calls, capped by KP_TAILORING_MAX_LISTINGS).

When ``tailoring_max_listings`` is 0 the node is a no-op passthrough.
When set, listings are sorted by salary (salary_max desc, then salary_min
desc; unknowns last) and only the top-N remain in ``qualified_listings``.
The rest are marked SKIPPED so tracking/notification can still report them.
"""

from __future__ import annotations

import structlog

from core.config import get_settings
from pipelines.job_agent.models import ApplicationStatus, JobListing
from pipelines.job_agent.state import JobAgentState, PipelinePhase

logger = structlog.get_logger(__name__)


def _salary_sort_key(listing: JobListing) -> tuple[int, int]:
    """Descending salary key — None treated as 0 so unknowns sort last."""
    return (-(listing.salary_max or 0), -(listing.salary_min or 0))


async def ranking_node(state: JobAgentState) -> JobAgentState:
    """Cap expensive tailoring to the top-N listings ranked by salary.

    Args:
        state: State with ``qualified_listings`` and ``job_analyses`` populated.

    Returns:
        State with ``qualified_listings`` trimmed to at most
        ``tailoring_max_listings`` entries (0 = keep all).
    """
    state.phase = PipelinePhase.RANKING
    cap = get_settings().tailoring_max_listings

    if not cap:
        logger.info("ranking.passthrough", total=len(state.qualified_listings))
        return state

    if not state.qualified_listings:
        return state

    sorted_listings = sorted(state.qualified_listings, key=_salary_sort_key)
    top_n = sorted_listings[:cap]
    skipped = sorted_listings[cap:]

    for listing in skipped:
        listing.status = ApplicationStatus.SKIPPED

    state.qualified_listings = top_n

    logger.info(
        "ranking.complete",
        cap=cap,
        selected=len(top_n),
        skipped=len(skipped),
        top=[
            {
                "company": listing.company,
                "title": listing.title,
                "salary_max": listing.salary_max,
                "salary_min": listing.salary_min,
            }
            for listing in top_n[:5]
        ],
    )
    return state
