"""Discovery node -- aggregate job listings from all configured sources.

Orchestrates browser and HTTP providers to collect ListingRef objects, then:
1. Deduplicates against in-run seen set and existing DB records.
2. Applies rule-based prefilter scoring.
3. Converts ListingRef -> JobListing (minimal, no description yet).
4. Writes state.discovered_listings.

Note: JobListing.description is NOT populated here. The bulk_extraction_node
(next in the graph) fetches full job page content for each qualified listing
after filtering has reduced the set.
"""

from __future__ import annotations

import structlog

from core.config import get_settings
from pipelines.job_agent.discovery.deduplication import deduplicate_refs
from pipelines.job_agent.discovery.models import DiscoveryConfig, ref_to_job_listing
from pipelines.job_agent.discovery.orchestrator import DiscoveryOrchestrator
from pipelines.job_agent.discovery.prefilter import apply_prefilter
from pipelines.job_agent.models import JobSource
from pipelines.job_agent.state import JobAgentState, PipelinePhase

logger = structlog.get_logger(__name__)


async def discovery_node(state: JobAgentState) -> JobAgentState:
    """Discover job listings from all configured provider sources."""
    state.phase = PipelinePhase.DISCOVERY
    settings = get_settings()
    config = DiscoveryConfig.from_settings(settings)

    if state.dry_run:
        logger.info("discovery.skip_dry_run")
        state.discovered_listings = []
        return state

    in_run_seen: set[str] = set()
    orchestrator = DiscoveryOrchestrator()

    refs = await orchestrator.run(state.search_criteria, config, settings, run_id=state.run_id)

    provider_results = getattr(orchestrator, "last_provider_results", [])
    for result in provider_results:
        for err in result.errors:
            state.errors.append(
                {
                    "node": "discovery",
                    "provider": result.source.value,
                    "message": err[:500],
                }
            )

    refs = await deduplicate_refs(refs, in_run_seen=in_run_seen, check_db=True)

    passed, rejected = apply_prefilter(
        refs, state.search_criteria, min_score=config.prefilter_min_score
    )

    logger.info(
        "discovery.prefilter_results",
        total=len(refs),
        passed=len(passed),
        rejected=len(rejected),
        min_score=config.prefilter_min_score,
    )

    listings = [ref_to_job_listing(ref) for ref in passed]
    state.discovered_listings = listings

    source_counts = {
        s.value: sum(1 for r in passed if r.source == s)
        for s in JobSource
        if any(r.source == s for r in passed)
    }
    logger.info(
        "discovery.complete",
        total_discovered=len(listings),
        sources=source_counts,
    )

    return state
