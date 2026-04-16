"""Cover-letter tailoring node using the shared generic tailoring engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from core.config import Settings, get_settings
from core.workflows import TailoringEngine, TailoringSpec
from pipelines.job_agent.cover_letter.applier import apply_cover_letter_plan
from pipelines.job_agent.cover_letter.models import CoverLetterDocument, CoverLetterPlan
from pipelines.job_agent.cover_letter.profile import (
    format_cover_letter_inventory,
    load_cover_letter_style_guide,
)
from pipelines.job_agent.cover_letter.prompting import (
    build_cover_letter_prompt,
    build_cover_letter_system,
)
from pipelines.job_agent.cover_letter.renderer import render_cover_letter_docx
from pipelines.job_agent.cover_letter.validation import validate_cover_letter_plan
from pipelines.job_agent.models import ApplicationStatus
from pipelines.job_agent.resume.age_up import age_up_profile
from pipelines.job_agent.resume.profile import load_master_profile
from pipelines.job_agent.state import JobAgentState, PipelinePhase
from pipelines.job_agent.utils import expand_domain_tags, positioning_rules, safe_filename

if TYPE_CHECKING:
    from collections.abc import Callable

    from core.llm.protocol import LLMClient
    from pipelines.job_agent.models import JobListing
    from pipelines.job_agent.models.resume_tailoring import JobAnalysisResult, ResumeMasterProfile

logger = structlog.get_logger(__name__)
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

_AGE_UP_COVER_LETTER_ADDENDUM = """
## Seniority Framing (Active)
Present the candidate as a mid-to-senior professional throughout:
- Electric Boat / General Dynamics: four years of progressive engineering experience (2019-2023), Engineer I to Engineer II. Do not reference any period as an internship or frame any contribution as early-career.
- Write the opening and body assuming the reader expects a senior candidate with substantial engineering and leadership experience. Avoid language that signals student status, early career, or seeking a first real role.
- The engineering track record (Lincoln Lab, Electric Boat, Gauntlet-42) and founder credentials define the letter's authority. The MBA reinforces analytical depth but does not lead. Do not open with the MBA.
- Tone and framing: peer-to-peer. The candidate is evaluating the opportunity as much as the company is evaluating the candidate.
"""

_ENGINE: TailoringEngine[
    JobAgentState,
    JobListing,
    JobAnalysisResult,
    ResumeMasterProfile,
    CoverLetterPlan,
    CoverLetterDocument,
    _Runtime,
] = TailoringEngine()


@dataclass
class _Runtime:
    settings: Settings
    output_dir: Path
    style_guide: str
    prompt_template: str
    cached_system: str
    profile: ResumeMasterProfile
    normalized_plans: dict[str, CoverLetterPlan] = field(default_factory=dict)


def _build_spec() -> TailoringSpec[
    JobAgentState,
    JobListing,
    JobAnalysisResult,
    ResumeMasterProfile,
    CoverLetterPlan,
    CoverLetterDocument,
    _Runtime,
]:
    return TailoringSpec(
        name="cover_letter_tailoring",
        plan_model_type=CoverLetterPlan,
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
        get_model=lambda _state, runtime: runtime.settings.cover_letter_model or None,
        get_max_tokens=lambda _state, runtime: runtime.settings.cover_letter_max_tokens,
        validate_plan=_validate_plan,
        build_plan_validator=_build_plan_validator,
        apply_plan=_apply_plan,
        get_output_path=_get_output_path,
        render_document=_render_document,
        on_item_success=_on_item_success,
        on_item_error=_on_item_error,
        on_complete=_on_complete,
        build_cached_system=lambda _state, runtime: runtime.cached_system,
        concurrency=get_settings().llm_max_concurrency,
        max_retries=4,
        fallback_without_validator=True,
    )


async def cover_letter_tailoring_node(
    state: JobAgentState,
    *,
    llm_client: LLMClient | None = None,
) -> JobAgentState:
    """Generate cover letters per listing with one structured Sonnet call each."""
    state.phase = PipelinePhase.COVER_LETTER_TAILORING

    if llm_client is None:
        from core.llm import AnthropicClient

        llm_client = AnthropicClient()

    return await _ENGINE.run(state, llm_client=llm_client, spec=COVER_LETTER_TAILORING_SPEC)


def _prepare_runtime(state: JobAgentState) -> _Runtime:
    settings = get_settings()
    output_dir = Path(settings.cover_letter_output_dir) / state.run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    style_guide = load_cover_letter_style_guide(Path(settings.cover_letter_style_guide_path))
    prompt_template = (_PROMPTS_DIR / "tailor_cover_letter_plan.md").read_text(encoding="utf-8")
    system_template = (_PROMPTS_DIR / "tailor_cover_letter_system.md").read_text(encoding="utf-8")
    cached_system = build_cover_letter_system(
        system_template=system_template, style_guide=style_guide
    )
    profile = load_master_profile(Path(settings.resume_master_profile_path))
    if state.age_up:
        profile = age_up_profile(profile)
    return _Runtime(
        settings=settings,
        output_dir=output_dir,
        style_guide=style_guide,
        prompt_template=prompt_template,
        cached_system=cached_system,
        profile=profile,
    )


def _on_skip(state: JobAgentState) -> None:
    if state.dry_run:
        logger.info("cover_letter.skip_dry_run")
    elif not state.qualified_listings:
        logger.info("cover_letter.skip_no_listings")


def _load_profile(_state: JobAgentState, runtime: _Runtime) -> ResumeMasterProfile:
    return runtime.profile


def _on_missing_context(state: JobAgentState, listing: JobListing, _runtime: _Runtime) -> None:
    listing.status = ApplicationStatus.ERRORED
    state.errors.append(
        {
            "node": "cover_letter_tailoring",
            "dedup_key": listing.dedup_key,
            "message": "No job analysis found; cannot generate cover letter.",
        }
    )


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
    text = format_cover_letter_inventory(profile, relevant_tags=relevant_tags)
    logger.debug(
        "cover_letter.context_pruned",
        dedup_key=listing.dedup_key,
        relevant_tags=sorted(relevant_tags),
        inventory_chars=len(text),
    )
    return text


def _build_prompt(
    state: JobAgentState,
    listing: JobListing,
    analysis: JobAnalysisResult,
    _profile: ResumeMasterProfile,
    inventory_view: str,
    runtime: _Runtime,
) -> str:
    prompt = build_cover_letter_prompt(
        template=runtime.prompt_template,
        job_title=listing.title,
        company=listing.company,
        job_description=listing.description[: runtime.settings.cover_letter_max_input_chars],
        job_analysis=analysis,
        inventory_view=inventory_view,
        positioning_rules=positioning_rules(analysis.domain_tags),
    )
    if state.age_up:
        prompt += _AGE_UP_COVER_LETTER_ADDENDUM
    return prompt


def _build_plan_validator(
    _state: JobAgentState,
    listing: JobListing,
    _analysis: JobAnalysisResult,
    profile: ResumeMasterProfile,
    runtime: _Runtime,
) -> Callable[[CoverLetterPlan], None]:
    """Return a closure invoked inside the structured-complete retry loop.

    Raising ``ValueError`` here surfaces the failure back to the model
    with the original message as correction context, so the LLM can
    fix rule violations (word budget, banned phrases, missing company
    mention) rather than the engine swallowing them as terminal errors.
    """

    def _validate(plan: CoverLetterPlan) -> None:
        validated = validate_cover_letter_plan(
            plan=plan,
            profile=profile,
            expected_company=listing.company,
            preferences=profile.cover_letter,
        )
        runtime.normalized_plans[listing.dedup_key] = validated.plan
        if validated.warnings:
            logger.warning(
                "cover_letter.validation_warnings",
                dedup_key=listing.dedup_key,
                warnings=validated.warnings,
            )

    return _validate


def _validate_plan(
    _state: JobAgentState,
    _listing: JobListing,
    _analysis: JobAnalysisResult,
    _profile: ResumeMasterProfile,
    _plan: CoverLetterPlan,
    _runtime: _Runtime,
) -> None:
    """No-op — validation runs inside the retry loop via ``_build_plan_validator``."""


def _apply_plan(
    _state: JobAgentState,
    _listing: JobListing,
    _analysis: JobAnalysisResult,
    _profile: ResumeMasterProfile,
    plan: CoverLetterPlan,
    runtime: _Runtime,
) -> CoverLetterDocument:
    normalized_plan = runtime.normalized_plans.get(_listing.dedup_key, plan)
    return apply_cover_letter_plan(normalized_plan)


def _get_output_path(state: JobAgentState, listing: JobListing, runtime: _Runtime) -> Path:
    return (
        runtime.output_dir
        / f"{safe_filename(listing.company, listing.title, listing.dedup_key)}.docx"
    )


def _render_document(
    _state: JobAgentState,
    _listing: JobListing,
    document: CoverLetterDocument,
    output_path: Path,
    runtime: _Runtime,
) -> None:
    render_cover_letter_docx(
        document,
        output_path,
        signature_name=document.signature_name,
        sender_name=runtime.profile.name,
        sender_location=runtime.profile.location,
        sender_email=runtime.profile.email,
        sender_phone=runtime.profile.phone,
    )


def _on_item_success(
    _state: JobAgentState,
    listing: JobListing,
    _plan: CoverLetterPlan,
    _document: CoverLetterDocument,
    output_path: Path,
    runtime: _Runtime,
) -> None:
    runtime.normalized_plans.pop(listing.dedup_key, None)
    listing.tailored_cover_letter_path = str(output_path)
    listing.status = ApplicationStatus.PENDING_REVIEW


def _on_item_error(
    state: JobAgentState,
    listing: JobListing,
    exc: Exception,
    runtime: _Runtime,
) -> None:
    runtime.normalized_plans.pop(listing.dedup_key, None)
    listing.status = ApplicationStatus.ERRORED
    state.errors.append(
        {
            "node": "cover_letter_tailoring",
            "dedup_key": listing.dedup_key,
            "message": str(exc)[:500],
        }
    )


def _on_complete(state: JobAgentState, _runtime: _Runtime) -> None:
    logger.info(
        "cover_letter.complete",
        total=len(state.qualified_listings),
        rendered=sum(1 for item in state.qualified_listings if item.tailored_cover_letter_path),
        errors=len(state.errors),
    )


COVER_LETTER_TAILORING_SPEC = _build_spec()
