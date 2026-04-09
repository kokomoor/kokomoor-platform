"""Tailoring node — generate per-listing tailored resumes.

Consumes pre-computed ``JobAnalysisResult`` objects from
``state.job_analyses`` (produced by the upstream job-analysis node)
and runs the remaining pipeline phases:

1. **Tailoring plan** — select/order/rewrite bullets referencing master profile IDs.
2. **Apply plan** — deterministic assembly from master profile + plan.
3. **Render .docx** — write the tailored resume to disk.

Each listing is processed independently; a failure on one listing
does not block the others.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from core.config import Settings, get_settings
from core.workflows import TailoringEngine, TailoringSpec
from pipelines.job_agent.models import ApplicationStatus
from pipelines.job_agent.models.resume_tailoring import (
    JobAnalysisResult,
    ResumeMasterProfile,
    ResumeTailoringPlan,
    TailoredResumeDocument,
)
from pipelines.job_agent.resume.applier import apply_tailoring_plan
from pipelines.job_agent.resume.profile import format_profile_for_llm, load_master_profile
from pipelines.job_agent.resume.renderer import render_resume_docx
from pipelines.job_agent.state import JobAgentState, PipelinePhase
from pipelines.job_agent.utils import expand_domain_tags, positioning_rules, safe_filename

if TYPE_CHECKING:
    from core.llm.protocol import LLMClient
    from pipelines.job_agent.models import JobListing

logger = structlog.get_logger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

_ENGINE: TailoringEngine[
    JobAgentState,
    JobListing,
    JobAnalysisResult,
    ResumeMasterProfile,
    ResumeTailoringPlan,
    TailoredResumeDocument,
    _Runtime,
] = TailoringEngine()


@dataclass(frozen=True)
class _Runtime:
    settings: Settings
    plan_template: str
    output_dir: Path


def _build_spec() -> TailoringSpec[
    JobAgentState,
    JobListing,
    JobAnalysisResult,
    ResumeMasterProfile,
    ResumeTailoringPlan,
    TailoredResumeDocument,
    _Runtime,
]:
    return TailoringSpec(
        name="resume_tailoring",
        plan_model_type=ResumeTailoringPlan,
        prepare=_prepare_runtime,
        should_skip=lambda state: state.dry_run or not state.qualified_listings,
        on_skip=_on_skip,
        get_items=lambda state: state.qualified_listings,
        load_inventory=_load_profile,
        get_context=lambda state, item, _runtime: state.job_analyses.get(item.dedup_key),
        on_missing_context=_on_missing_context,
        on_item_start=_on_item_start,
        build_inventory_view=_build_inventory_view,
        build_prompt=_build_prompt,
        get_run_id=lambda state: state.run_id,
        get_model=lambda _state, runtime: runtime.settings.resume_plan_model or None,
        get_max_tokens=lambda _state, runtime: runtime.settings.resume_plan_max_tokens,
        validate_plan=_validate_plan,
        apply_plan=_apply_plan,
        get_output_path=_get_output_path,
        render_document=_render_document,
        on_item_success=_on_item_success,
        on_item_error=_on_item_error,
        on_complete=_on_complete,
    )


async def tailoring_node(
    state: JobAgentState,
    *,
    llm_client: LLMClient | None = None,
) -> JobAgentState:
    """Tailor resumes for every listing in ``state.qualified_listings``."""
    state.phase = PipelinePhase.TAILORING

    if llm_client is None:
        from core.llm import AnthropicClient

        llm_client = AnthropicClient()

    return await _ENGINE.run(state, llm_client=llm_client, spec=RESUME_TAILORING_SPEC)


def _prepare_runtime(state: JobAgentState) -> _Runtime:
    settings = get_settings()
    output_dir = Path(settings.resume_output_dir) / state.run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    plan_template = (_PROMPTS_DIR / "tailor_resume_plan.md").read_text(encoding="utf-8")
    return _Runtime(settings=settings, plan_template=plan_template, output_dir=output_dir)


def _on_skip(state: JobAgentState) -> None:
    if state.dry_run:
        logger.info("tailoring.skip_dry_run")
        state.tailored_listings = state.qualified_listings
    elif not state.qualified_listings:
        logger.info("tailoring.skip_no_listings")
        state.tailored_listings = []


def _load_profile(_state: JobAgentState, runtime: _Runtime) -> ResumeMasterProfile:
    return load_master_profile(Path(runtime.settings.resume_master_profile_path))


def _on_missing_context(state: JobAgentState, listing: JobListing, _runtime: _Runtime) -> None:
    listing.status = ApplicationStatus.ERRORED
    state.errors.append(
        {
            "node": "tailoring",
            "dedup_key": listing.dedup_key,
            "message": "No job analysis found; job_analysis node may have failed.",
        }
    )
    logger.warning("tailoring.missing_analysis", dedup_key=listing.dedup_key)


def _on_item_start(_state: JobAgentState, listing: JobListing, _runtime: _Runtime) -> None:
    listing.status = ApplicationStatus.TAILORING


def _build_inventory_view(
    _state: JobAgentState,
    listing: JobListing,
    analysis: JobAnalysisResult,
    profile: ResumeMasterProfile,
    _runtime: _Runtime,
) -> str:
    relevant_tags = expand_domain_tags(analysis.domain_tags)
    profile_text = format_profile_for_llm(profile, relevant_tags=relevant_tags)
    logger.debug(
        "tailoring.context_pruned",
        dedup_key=listing.dedup_key,
        relevant_tags=sorted(relevant_tags),
        profile_chars=len(profile_text),
    )
    return profile_text


def _build_prompt(
    _state: JobAgentState,
    listing: JobListing,
    analysis: JobAnalysisResult,
    _profile: ResumeMasterProfile,
    inventory_view: str,
    runtime: _Runtime,
) -> str:
    prompt = runtime.plan_template.format(
        job_analysis=analysis.model_dump_json(indent=2),
        candidate_profile_structured=inventory_view,
        positioning_rules=positioning_rules(analysis.domain_tags),
    )
    logger.debug("tailoring.prompt_built", dedup_key=listing.dedup_key)
    return prompt


def _validate_plan(
    _state: JobAgentState,
    _listing: JobListing,
    _analysis: JobAnalysisResult,
    profile: ResumeMasterProfile,
    plan: ResumeTailoringPlan,
    _runtime: _Runtime,
) -> None:
    valid_bullet_ids = profile.all_bullet_ids()
    unknown_ids: list[str] = []
    for op in plan.bullet_ops:
        if op.bullet_id not in valid_bullet_ids:
            unknown_ids.append(op.bullet_id)
    if unknown_ids:
        logger.warning(
            "tailoring.plan_unknown_bullet_ops",
            dedup_key=_listing.dedup_key,
            unknown_bullet_ids=unknown_ids,
        )


def _apply_plan(
    _state: JobAgentState,
    _listing: JobListing,
    _analysis: JobAnalysisResult,
    profile: ResumeMasterProfile,
    plan: ResumeTailoringPlan,
    _runtime: _Runtime,
) -> TailoredResumeDocument:
    return apply_tailoring_plan(profile, plan)


def _get_output_path(state: JobAgentState, listing: JobListing, runtime: _Runtime) -> Path:
    return (
        runtime.output_dir
        / f"{safe_filename(listing.company, listing.title, listing.dedup_key)}.docx"
    )


def _render_document(
    _state: JobAgentState,
    _listing: JobListing,
    document: TailoredResumeDocument,
    output_path: Path,
    _runtime: _Runtime,
) -> None:
    render_resume_docx(document, output_path)


def _on_item_success(
    _state: JobAgentState,
    listing: JobListing,
    plan: ResumeTailoringPlan,
    _document: TailoredResumeDocument,
    output_path: Path,
    _runtime: _Runtime,
) -> None:
    logger.info(
        "tailoring.plan_created",
        dedup_key=listing.dedup_key,
        experience_sections=len(plan.experience_sections),
        bullet_ops=len(plan.bullet_ops),
    )
    listing.tailored_resume_path = str(output_path)
    listing.status = ApplicationStatus.PENDING_REVIEW
    logger.info(
        "tailoring.listing_complete",
        dedup_key=listing.dedup_key,
        path=str(output_path),
    )


def _on_item_error(
    state: JobAgentState, listing: JobListing, exc: Exception, _runtime: _Runtime
) -> None:
    listing.status = ApplicationStatus.ERRORED
    state.errors.append(
        {
            "node": "tailoring",
            "dedup_key": listing.dedup_key,
            "message": str(exc)[:500],
        }
    )
    logger.warning(
        "tailoring.listing_failed",
        dedup_key=listing.dedup_key,
        error=str(exc)[:200],
    )


def _on_complete(state: JobAgentState, _runtime: _Runtime) -> None:
    state.tailored_listings = state.qualified_listings
    tailored_count = sum(
        1 for li in state.qualified_listings if li.tailored_resume_path is not None
    )
    logger.info(
        "tailoring.complete",
        total=len(state.qualified_listings),
        tailored=tailored_count,
        errors=len(state.errors),
    )


RESUME_TAILORING_SPEC = _build_spec()
