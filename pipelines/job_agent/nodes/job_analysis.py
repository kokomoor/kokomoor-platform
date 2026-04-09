"""Job analysis node — extract structured understanding from full job descriptions.

Dedicated LangGraph node that sits between extraction and tailoring.
Reads the full ``JobListing.description`` (no truncation), produces a
``JobAnalysisResult`` per listing, and stores results on
``state.job_analyses`` keyed by ``dedup_key``.

The tailoring node then consumes these pre-computed analyses instead of
running its own embedded LLM pass.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from core.config import get_settings
from core.llm.structured import structured_complete
from pipelines.job_agent.models import ApplicationStatus
from pipelines.job_agent.models.resume_tailoring import JobAnalysisResult
from pipelines.job_agent.state import JobAgentState, PipelinePhase

if TYPE_CHECKING:
    from core.llm.protocol import LLMClient
    from pipelines.job_agent.models import JobListing

logger = structlog.get_logger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


async def job_analysis_node(
    state: JobAgentState,
    *,
    llm_client: LLMClient | None = None,
) -> JobAgentState:
    """Analyse every listing in ``qualified_listings`` and populate ``job_analyses``.

    Skips listings that already have a cached analysis (by ``dedup_key``).
    """
    state.phase = PipelinePhase.JOB_ANALYSIS

    if state.dry_run:
        logger.info("job_analysis.skip_dry_run")
        return state

    if not state.qualified_listings:
        logger.info("job_analysis.skip_no_listings")
        return state

    if llm_client is None:
        from core.llm import AnthropicClient

        llm_client = AnthropicClient()

    settings = get_settings()
    prompt_template = (_PROMPTS_DIR / "tailor_job_analysis.md").read_text(encoding="utf-8")
    model = settings.job_analysis_model or None
    max_tokens = settings.job_analysis_max_tokens
    max_input_chars = settings.job_analysis_max_input_chars
    enable_cache = settings.job_analysis_enable_cache

    for listing in state.qualified_listings:
        cache_key = _analysis_cache_key(listing)
        if enable_cache and cache_key in state.job_analysis_cache:
            logger.info("job_analysis.cache_hit", dedup_key=listing.dedup_key, cache_key=cache_key)
            state.job_analyses[listing.dedup_key] = state.job_analysis_cache[cache_key]
            continue

        try:
            analysis = await _analyse_listing(
                listing=listing,
                llm_client=llm_client,
                prompt_template=prompt_template,
                model=model,
                max_tokens=max_tokens,
                max_input_chars=max_input_chars,
                run_id=state.run_id,
            )
            state.job_analyses[listing.dedup_key] = analysis
            if enable_cache:
                state.job_analysis_cache[cache_key] = analysis
        except Exception as exc:
            listing.status = ApplicationStatus.ERRORED
            state.errors.append(
                {
                    "node": "job_analysis",
                    "dedup_key": listing.dedup_key,
                    "message": str(exc)[:500],
                }
            )
            logger.warning(
                "job_analysis.failed",
                dedup_key=listing.dedup_key,
                error=str(exc)[:200],
            )

    logger.info(
        "job_analysis.complete",
        total=len(state.qualified_listings),
        analysed=len(state.job_analyses),
        errors=len(state.errors),
    )
    return state


def _analysis_cache_key(listing: JobListing) -> str:
    """Cache key for analysis includes dedup key + description fingerprint."""
    desc_hash = hashlib.sha256((listing.description or "").encode("utf-8")).hexdigest()[:16]
    return f"{listing.dedup_key}:{desc_hash}"


async def _analyse_listing(
    *,
    listing: JobListing,
    llm_client: LLMClient,
    prompt_template: str,
    model: str | None,
    max_tokens: int,
    max_input_chars: int,
    run_id: str,
) -> JobAnalysisResult:
    """Run the LLM structured extraction for one listing."""
    if not listing.description:
        msg = f"Listing {listing.dedup_key} has empty description"
        raise ValueError(msg)

    listing.status = ApplicationStatus.ANALYZING

    jd_text = listing.description[:max_input_chars]

    prompt = prompt_template.format(
        job_title=listing.title,
        company=listing.company,
        job_description=jd_text,
    )

    analysis = await structured_complete(
        llm_client,
        prompt,
        response_model=JobAnalysisResult,
        model=model,
        max_tokens=max_tokens,
        run_id=run_id,
    )

    listing.status = ApplicationStatus.ANALYZED

    logger.info(
        "job_analysis.analysed",
        dedup_key=listing.dedup_key,
        themes=analysis.themes[:3],
        seniority=analysis.seniority,
        basic_quals=len(analysis.basic_qualifications),
        preferred_quals=len(analysis.preferred_qualifications),
        model_used=model or "default",
        input_chars=len(jd_text),
    )

    return analysis
