"""Bulk extraction node -- fetch full job descriptions for qualified listings.

Runs AFTER filtering (which reduces the listing count) and BEFORE job_analysis
(which needs ``listing.description`` to be populated).

Design:

- We fan out extraction with a bounded semaphore so several listings can be
  fetched in parallel without blowing past Playwright's context budget. The
  previous implementation ran strictly sequentially with a 1.5-4 s sleep
  between each listing, which turned 30 listings into two minutes of wall
  time for what is essentially IO-bound work.
- Each worker still adds a small per-task jitter so we do not hammer the
  same origin. The randomness sits inside the semaphore window rather than
  outside it, so concurrency is preserved.
- Failures on one listing do not cancel the others; they mark the listing
  ``ERRORED`` and continue.
"""

from __future__ import annotations

import asyncio
import random

import structlog

from core.config import get_settings
from pipelines.job_agent.extraction import extract_job_data_from_url
from pipelines.job_agent.models import ApplicationStatus, JobListing
from pipelines.job_agent.state import JobAgentState, PipelinePhase

logger = structlog.get_logger(__name__)


async def _extract_one(
    listing: JobListing,
    semaphore: asyncio.Semaphore,
    state: JobAgentState,
) -> bool:
    """Fetch one listing's description under the shared semaphore.

    Returns ``True`` if extraction succeeded (or was already populated),
    ``False`` on failure. Failures are recorded on ``state.errors``.
    """
    if listing.description:
        return True

    async with semaphore:
        # Jitter lives inside the semaphore window so we keep concurrency
        # but still avoid firing synchronised requests at the same origin.
        await asyncio.sleep(random.uniform(0.0, 1.5))
        try:
            extracted = await extract_job_data_from_url(listing.url)
        except Exception as exc:
            listing.status = ApplicationStatus.ERRORED
            state.errors.append(
                {
                    "node": "bulk_extraction",
                    "dedup_key": listing.dedup_key,
                    "message": str(exc)[:500],
                }
            )
            logger.warning(
                "bulk_extraction.listing_failed",
                dedup_key=listing.dedup_key,
                error=str(exc)[:200],
            )
            return False

    listing.description = extracted.cleaned_description
    if not listing.title or listing.title == "Unknown Role":
        listing.title = extracted.title or listing.title
    if not listing.company or listing.company == "Unknown Company":
        listing.company = extracted.company or listing.company
    if not listing.location:
        listing.location = extracted.location
    if listing.salary_min is None:
        listing.salary_min = extracted.salary_min
    if listing.salary_max is None:
        listing.salary_max = extracted.salary_max
    if listing.remote is None:
        listing.remote = extracted.remote
    listing.status = ApplicationStatus.DISCOVERED
    logger.info(
        "bulk_extraction.listing_complete",
        dedup_key=listing.dedup_key,
        description_chars=len(listing.description),
        source=listing.source.value,
    )
    return True


async def bulk_extraction_node(state: JobAgentState) -> JobAgentState:
    """Fan out page-content extraction across qualified listings."""
    state.phase = PipelinePhase.BULK_EXTRACTION

    if state.dry_run or not state.qualified_listings:
        logger.info(
            "bulk_extraction.skip",
            dry_run=state.dry_run,
            listings=len(state.qualified_listings),
        )
        return state

    settings = get_settings()
    # Re-use the LLM concurrency knob as a sane default for network-bound
    # extraction parallelism. A dedicated setting would be overkill for
    # the current pipeline size and keeps the knob surface small.
    concurrency = max(1, settings.llm_max_concurrency)
    semaphore = asyncio.Semaphore(concurrency)

    results = await asyncio.gather(
        *(_extract_one(listing, semaphore, state) for listing in state.qualified_listings),
        return_exceptions=False,
    )
    success_count = sum(1 for ok in results if ok)

    logger.info(
        "bulk_extraction.complete",
        total=len(state.qualified_listings),
        success=success_count,
        errors=len(state.errors),
        concurrency=concurrency,
    )
    return state
