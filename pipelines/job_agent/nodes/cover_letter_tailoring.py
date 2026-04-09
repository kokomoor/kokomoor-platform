"""Cover-letter tailoring node using the shared generic tailoring engine."""

from __future__ import annotations

from dataclasses import dataclass
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
from pipelines.job_agent.cover_letter.prompting import build_cover_letter_prompt
from pipelines.job_agent.cover_letter.renderer import render_cover_letter_docx
from pipelines.job_agent.cover_letter.validation import validate_cover_letter_plan
from pipelines.job_agent.models import ApplicationStatus
from pipelines.job_agent.resume.profile import load_master_profile
from pipelines.job_agent.state import JobAgentState, PipelinePhase

if TYPE_CHECKING:
    from core.llm.protocol import LLMClient
    from pipelines.job_agent.models import JobListing
    from pipelines.job_agent.models.resume_tailoring import JobAnalysisResult, ResumeMasterProfile

logger = structlog.get_logger(__name__)
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

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
    sender_name: str
    sender_location: str
    sender_email: str
    sender_phone: str
    normalized_plans: dict[str, CoverLetterPlan]


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
        apply_plan=_apply_plan,
        get_output_path=_get_output_path,
        render_document=_render_document,
        on_item_success=_on_item_success,
        on_item_error=_on_item_error,
        on_complete=_on_complete,
    )


async def cover_letter_tailoring_node(
    state: JobAgentState,
    *,
    llm_client: LLMClient | None = None,
) -> JobAgentState:
    """Generate cover letters per listing with one structured Sonnet call each."""
    state.phase = PipelinePhase.TAILORING

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
    profile = load_master_profile(Path(settings.resume_master_profile_path))
    return _Runtime(
        settings=settings,
        output_dir=output_dir,
        style_guide=style_guide,
        prompt_template=prompt_template,
        sender_name=profile.name,
        sender_location=profile.location,
        sender_email=profile.email,
        sender_phone=profile.phone,
        normalized_plans={},
    )


def _on_skip(state: JobAgentState) -> None:
    if state.dry_run:
        logger.info("cover_letter.skip_dry_run")
    elif not state.qualified_listings:
        logger.info("cover_letter.skip_no_listings")


def _load_profile(_state: JobAgentState, runtime: _Runtime) -> ResumeMasterProfile:
    return load_master_profile(Path(runtime.settings.resume_master_profile_path))


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
    _listing: JobListing,
    _analysis: JobAnalysisResult,
    profile: ResumeMasterProfile,
    _runtime: _Runtime,
) -> str:
    return format_cover_letter_inventory(profile)


def _build_prompt(
    _state: JobAgentState,
    listing: JobListing,
    analysis: JobAnalysisResult,
    _profile: ResumeMasterProfile,
    inventory_view: str,
    runtime: _Runtime,
) -> str:
    return build_cover_letter_prompt(
        template=runtime.prompt_template,
        job_title=listing.title,
        company=listing.company,
        job_description=listing.description[: runtime.settings.cover_letter_max_input_chars],
        job_analysis=analysis,
        inventory_view=inventory_view,
        style_guide=runtime.style_guide,
    )


def _validate_plan(
    _state: JobAgentState,
    listing: JobListing,
    _analysis: JobAnalysisResult,
    profile: ResumeMasterProfile,
    plan: CoverLetterPlan,
    runtime: _Runtime,
) -> None:
    validated = validate_cover_letter_plan(
        plan=plan,
        profile=profile,
        expected_company=listing.company,
        preferences=profile.cover_letter,
    )
    runtime.normalized_plans[listing.dedup_key] = validated.plan


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
    safe = _safe_filename(listing.company, listing.title, listing.dedup_key)
    return runtime.output_dir / f"{safe}.docx"


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
        sender_name=runtime.sender_name,
        sender_location=runtime.sender_location,
        sender_email=runtime.sender_email,
        sender_phone=runtime.sender_phone,
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


def _safe_filename(company: str, title: str, dedup_key: str) -> str:
    raw = f"{company}_{title}".replace(" ", "_")
    safe = "".join(c for c in raw if c.isalnum() or c in ("_", "-"))
    return f"{safe[:50]}_{dedup_key[:8]}"


COVER_LETTER_TAILORING_SPEC = _build_spec()
