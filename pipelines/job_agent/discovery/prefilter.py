"""Lightweight rule-based fit scoring before expensive LLM analysis.

NOT an LLM call. Pure string matching against SearchCriteria. The score
gate is intentionally low by default (0.0 = accept everything) to avoid
false negatives. Raise the threshold only if discovery is returning large
volumes of clearly irrelevant roles.

This runs BEFORE deduplication-based DB writes and before description
fetching — it's the cheapest possible gate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from pipelines.job_agent.discovery.models import ListingRef
    from pipelines.job_agent.models import SearchCriteria

logger = structlog.get_logger(__name__)

TITLE_DISQUALIFIERS: frozenset[str] = frozenset(
    {
        "intern",
        "internship",
        "contract",
        "part-time",
        "part time",
        "junior",
        "entry level",
        "entry-level",
        "associate",
        "coordinator",
        "assistant",
        "administrative",
        "support specialist",
        "data entry",
    }
)


def score_listing_ref(ref: ListingRef, criteria: SearchCriteria) -> float:
    """Score a listing ref against search criteria (0.0-1.0)."""
    score = 0.0
    title_lower = ref.title.lower()
    company_lower = ref.company.lower()
    location_lower = ref.location.lower()

    if criteria.target_roles and any(role.lower() in title_lower for role in criteria.target_roles):
        score += 0.40

    keyword_hits = 0
    for kw in criteria.keywords:
        if kw.lower() in title_lower:
            keyword_hits += 1
    score += min(keyword_hits * 0.10, 0.20)

    if criteria.target_companies and any(
        co.lower() in company_lower for co in criteria.target_companies
    ):
        score += 0.35

    location_matched = (
        criteria.locations and any(loc.lower() in location_lower for loc in criteria.locations)
    ) or (criteria.remote_ok and "remote" in location_lower)
    if location_matched:
        score += 0.10

    if any(dq in title_lower for dq in TITLE_DISQUALIFIERS):
        score -= 0.60

    return max(0.0, min(score, 1.0))


def apply_prefilter(
    refs: list[ListingRef],
    criteria: SearchCriteria,
    *,
    min_score: float,
) -> tuple[list[ListingRef], list[ListingRef]]:
    """Split refs into (passed, rejected) based on fit score threshold."""
    if min_score <= 0.0:
        logger.info("prefilter_bypass", total=len(refs), reason="min_score<=0")
        return refs, []

    passed: list[ListingRef] = []
    rejected: list[ListingRef] = []
    for ref in refs:
        s = score_listing_ref(ref, criteria)
        if s >= min_score:
            passed.append(ref)
        else:
            rejected.append(ref)

    logger.info(
        "prefilter_complete",
        total=len(refs),
        passed=len(passed),
        rejected=len(rejected),
        min_score=min_score,
    )
    return passed, rejected
