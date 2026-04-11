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

    raw_by_source = _count_by_source(refs)

    refs = await deduplicate_refs(refs, in_run_seen=in_run_seen, check_db=True)
    deduped_by_source = _count_by_source(refs)

    passed, rejected = apply_prefilter(
        refs, state.search_criteria, min_score=config.prefilter_min_score
    )
    passed_by_source = _count_by_source(passed)

    # Per-source funnel: raw → deduped → prefiltered. Without this, a
    # provider that returns 124 refs but only contributes 30 looks like
    # under-performance instead of aggressive deduplication or rejection.
    for source in sorted(raw_by_source.keys() | deduped_by_source.keys()):
        raw = raw_by_source.get(source, 0)
        deduped = deduped_by_source.get(source, 0)
        kept = passed_by_source.get(source, 0)
        logger.info(
            "discovery.source_funnel",
            source=source,
            raw=raw,
            deduped=deduped,
            dedup_dropped=raw - deduped,
            prefilter_dropped=deduped - kept,
            kept=kept,
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

    logger.info(
        "discovery.complete",
        total_discovered=len(listings),
        sources=passed_by_source,
    )

    return state


def _count_by_source(refs: list) -> dict[str, int]:  # type: ignore[type-arg]
    counts: dict[str, int] = {}
    for ref in refs:
        key = ref.source.value if isinstance(ref.source, JobSource) else str(ref.source)
        counts[key] = counts.get(key, 0) + 1
    return counts
