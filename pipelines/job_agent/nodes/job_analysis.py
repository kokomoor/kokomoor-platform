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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from core.config import Settings, get_settings
from core.workflows import StructuredAnalysisEngine, StructuredAnalysisSpec
from pipelines.job_agent.models import ApplicationStatus
from pipelines.job_agent.models.resume_tailoring import JobAnalysisResult
from pipelines.job_agent.state import JobAgentState, PipelinePhase

if TYPE_CHECKING:
    from core.llm.protocol import LLMClient
    from pipelines.job_agent.models import JobListing

logger = structlog.get_logger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_ENGINE: StructuredAnalysisEngine[JobAgentState, JobListing, JobAnalysisResult, _Runtime] = (
    StructuredAnalysisEngine()
)


@dataclass(frozen=True)
class _Runtime:
    prompt_template: str
    settings: Settings


def _build_spec() -> StructuredAnalysisSpec[JobAgentState, JobListing, JobAnalysisResult, _Runtime]:
    return StructuredAnalysisSpec(
        name="job_analysis",
        response_model=JobAnalysisResult,
        prepare=_prepare_runtime,
        get_items=lambda state: state.qualified_listings,
        should_skip=lambda state: state.dry_run or not state.qualified_listings,
        on_skip=_on_skip,
        build_prompt=_build_prompt,
        get_run_id=lambda state: state.run_id,
        get_model=lambda _state, runtime: runtime.settings.job_analysis_model or None,
        get_max_tokens=lambda _state, runtime: runtime.settings.job_analysis_max_tokens,
        get_cache_key=lambda _state, item, _runtime: _analysis_cache_key(item),
        get_cached_result=_get_cached_result,
        cache_result=_cache_result,
        on_item_start=_on_item_start,
        on_item_result=_on_item_result,
        on_item_error=_on_item_error,
        on_complete=_on_complete,
    )


async def job_analysis_node(
    state: JobAgentState,
    *,
    llm_client: LLMClient | None = None,
) -> JobAgentState:
    """Analyse every listing in ``qualified_listings`` and populate ``job_analyses``.

    Skips listings that already have a cached analysis (by ``dedup_key``).
    """
    state.phase = PipelinePhase.JOB_ANALYSIS

    if llm_client is None:
        from core.llm import AnthropicClient

        llm_client = AnthropicClient()

    return await _ENGINE.run(state, llm_client=llm_client, spec=JOB_ANALYSIS_SPEC)


def _prepare_runtime(_state: JobAgentState) -> _Runtime:
    settings = get_settings()
    prompt_template = (_PROMPTS_DIR / "tailor_job_analysis.md").read_text(encoding="utf-8")
    return _Runtime(prompt_template=prompt_template, settings=settings)


def _on_skip(state: JobAgentState) -> None:
    if state.dry_run:
        logger.info("job_analysis.skip_dry_run")
    elif not state.qualified_listings:
        logger.info("job_analysis.skip_no_listings")


def _analysis_cache_key(listing: JobListing) -> str:
    """Cache key for analysis includes dedup key + description fingerprint."""
    desc_hash = hashlib.sha256((listing.description or "").encode("utf-8")).hexdigest()[:16]
    return f"{listing.dedup_key}:{desc_hash}"


def _get_cached_result(
    state: JobAgentState, cache_key: str, runtime: _Runtime
) -> JobAnalysisResult | None:
    if not runtime.settings.job_analysis_enable_cache:
        return None
    return state.job_analysis_cache.get(cache_key)


def _cache_result(
    state: JobAgentState, cache_key: str, result: JobAnalysisResult, runtime: _Runtime
) -> None:
    if runtime.settings.job_analysis_enable_cache:
        state.job_analysis_cache[cache_key] = result


def _on_item_start(_state: JobAgentState, listing: JobListing, _runtime: _Runtime) -> None:
    if not listing.description:
        msg = f"Listing {listing.dedup_key} has empty description"
        raise ValueError(msg)
    listing.status = ApplicationStatus.ANALYZING


def _build_prompt(state: JobAgentState, listing: JobListing, runtime: _Runtime) -> str:
    jd_text = listing.description[: runtime.settings.job_analysis_max_input_chars]
    prompt = runtime.prompt_template.format(
        job_title=listing.title,
        company=listing.company,
        job_description=jd_text,
    )
    logger.debug(
        "job_analysis.prompt_built",
        dedup_key=listing.dedup_key,
        model=runtime.settings.job_analysis_model or "default",
        input_chars=len(jd_text),
        run_id=state.run_id,
    )
    return prompt


def _on_item_result(
    state: JobAgentState,
    listing: JobListing,
    result: JobAnalysisResult,
    runtime: _Runtime,
) -> None:
    state.job_analyses[listing.dedup_key] = result
    listing.status = ApplicationStatus.ANALYZED
    logger.info(
        "job_analysis.analysed",
        dedup_key=listing.dedup_key,
        themes=result.themes[:3],
        seniority=result.seniority,
        basic_quals=len(result.basic_qualifications),
        preferred_quals=len(result.preferred_qualifications),
        model_used=runtime.settings.job_analysis_model or "default",
        input_chars=min(len(listing.description), runtime.settings.job_analysis_max_input_chars),
    )


def _on_item_error(
    state: JobAgentState, listing: JobListing, exc: Exception, _runtime: _Runtime
) -> None:
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


def _on_complete(state: JobAgentState, _runtime: _Runtime) -> None:
    logger.info(
        "job_analysis.complete",
        total=len(state.qualified_listings),
        analysed=len(state.job_analyses),
        errors=len(state.errors),
    )


JOB_ANALYSIS_SPEC = _build_spec()
