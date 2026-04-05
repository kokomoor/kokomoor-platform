"""Discovery node — scrape and collect job listings.

This node is responsible for finding new job listings from configured
sources, parsing them into structured ``JobListing`` objects, and
deduplicating against previously seen listings.

Milestone 2 will flesh out the implementation with real Playwright
scraping and multiple source adapters.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import structlog

from pipelines.job_agent.state import JobAgentState, PipelinePhase

if TYPE_CHECKING:
    from pipelines.job_agent.models import JobListing

logger = structlog.get_logger(__name__)


def _generate_dedup_key(company: str, title: str, url: str) -> str:
    """Generate a deterministic dedup key from listing identifiers."""
    raw = f"{company.lower().strip()}|{title.lower().strip()}|{url.strip()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


async def discovery_node(state: JobAgentState) -> JobAgentState:
    """Discover job listings from configured sources.

    Currently a stub that demonstrates the node interface. Milestone 2
    will add real scraping via ``core.browser.BrowserManager`` and
    structured extraction via ``core.llm.structured``.

    Args:
        state: Current pipeline state with search criteria.

    Returns:
        Updated state with ``discovered_listings`` populated.
    """
    state.phase = PipelinePhase.DISCOVERY
    logger.info(
        "discovery_start",
        sources=[s.value for s in state.search_criteria.sources],
        keywords=state.search_criteria.keywords,
    )

    # --- STUB: replace with real scraping in Milestone 2 ---
    # For now, return an empty list to prove the pipeline runs end-to-end.
    discovered: list[JobListing] = []

    state.discovered_listings = discovered
    logger.info("discovery_complete", count=len(discovered))
    return state
