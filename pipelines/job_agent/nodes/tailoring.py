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

from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from core.config import get_settings
from core.llm.structured import structured_complete
from pipelines.job_agent.models import ApplicationStatus
from pipelines.job_agent.models.resume_tailoring import (
    JobAnalysisResult,
    ResumeMasterProfile,
    ResumeTailoringPlan,
)
from pipelines.job_agent.resume.applier import apply_tailoring_plan
from pipelines.job_agent.resume.profile import format_profile_for_llm, load_master_profile
from pipelines.job_agent.resume.renderer import render_resume_docx
from pipelines.job_agent.state import JobAgentState, PipelinePhase

if TYPE_CHECKING:
    from core.llm.protocol import LLMClient
    from pipelines.job_agent.models import JobListing

logger = structlog.get_logger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

_ALWAYS_RELEVANT_TAGS = {"leadership", "technical", "general", "management", "software"}

_TAG_EXPANSION: dict[str, list[str]] = {
    "military": ["defense", "naval"],
    "government": ["defense"],
    "aerospace": ["defense"],
    "robotics": ["technical", "hardware", "software"],
    "data": ["ml", "ai"],
    "machine learning": ["ml", "ai"],
    "fintech": ["finance"],
    "trading": ["finance"],
    "quant": ["finance", "math"],
    "nuclear": ["energy"],
    "clean": ["energy"],
    "climate": ["energy"],
    "product": ["startup"],
    "growth": ["startup"],
}


async def tailoring_node(
    state: JobAgentState,
    *,
    llm_client: LLMClient | None = None,
) -> JobAgentState:
    """Tailor resumes for every listing in ``state.qualified_listings``.

    Requires ``state.job_analyses`` to be populated by the upstream
    job-analysis node. Listings without a matching analysis are skipped
    with an error.
    """
    state.phase = PipelinePhase.TAILORING

    if state.dry_run:
        logger.info("tailoring.skip_dry_run")
        state.tailored_listings = state.qualified_listings
        return state

    if not state.qualified_listings:
        logger.info("tailoring.skip_no_listings")
        state.tailored_listings = []
        return state

    if llm_client is None:
        from core.llm import AnthropicClient

        llm_client = AnthropicClient()

    settings = get_settings()
    profile = load_master_profile(Path(settings.resume_master_profile_path))

    output_dir = Path(settings.resume_output_dir) / state.run_id
    output_dir.mkdir(parents=True, exist_ok=True)

    plan_template = (_PROMPTS_DIR / "tailor_resume_plan.md").read_text(encoding="utf-8")
    plan_model = settings.resume_plan_model or None
    plan_max_tokens = settings.resume_plan_max_tokens

    for listing in state.qualified_listings:
        analysis = state.job_analyses.get(listing.dedup_key)
        if analysis is None:
            state.errors.append(
                {
                    "node": "tailoring",
                    "dedup_key": listing.dedup_key,
                    "message": "No job analysis found; job_analysis node may have failed.",
                }
            )
            logger.warning("tailoring.missing_analysis", dedup_key=listing.dedup_key)
            continue

        try:
            await _tailor_listing(
                listing=listing,
                analysis=analysis,
                profile=profile,
                llm_client=llm_client,
                output_dir=output_dir,
                plan_template=plan_template,
                run_id=state.run_id,
                plan_model=plan_model,
                plan_max_tokens=plan_max_tokens,
            )
        except Exception as exc:
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
    return state


# ── per-listing orchestration ──────────────────────────────────────────


async def _tailor_listing(
    *,
    listing: JobListing,
    analysis: JobAnalysisResult,
    profile: ResumeMasterProfile,
    llm_client: LLMClient,
    output_dir: Path,
    plan_template: str,
    run_id: str,
    plan_model: str | None,
    plan_max_tokens: int,
) -> None:
    """Run the plan -> apply -> render pipeline for one listing."""
    listing.status = ApplicationStatus.TAILORING

    relevant_tags = _expand_domain_tags(analysis.domain_tags)
    profile_text = format_profile_for_llm(profile, relevant_tags=relevant_tags)

    logger.debug(
        "tailoring.context_pruned",
        dedup_key=listing.dedup_key,
        relevant_tags=sorted(relevant_tags),
        profile_chars=len(profile_text),
    )

    plan_prompt = plan_template.format(
        job_analysis=analysis.model_dump_json(indent=2),
        candidate_profile_structured=profile_text,
        positioning_rules=_positioning_rules(analysis.domain_tags),
    )
    plan = await structured_complete(
        llm_client,
        plan_prompt,
        response_model=ResumeTailoringPlan,
        model=plan_model,
        max_tokens=plan_max_tokens,
        run_id=run_id,
    )
    logger.info(
        "tailoring.plan_created",
        dedup_key=listing.dedup_key,
        experience_sections=len(plan.experience_sections),
        bullet_ops=len(plan.bullet_ops),
    )

    tailored_doc = apply_tailoring_plan(profile, plan)

    safe_name = _safe_filename(listing.company, listing.title, listing.dedup_key)
    out_path = output_dir / f"{safe_name}.docx"
    render_resume_docx(tailored_doc, out_path)

    listing.tailored_resume_path = str(out_path)
    listing.status = ApplicationStatus.PENDING_REVIEW

    logger.info(
        "tailoring.listing_complete",
        dedup_key=listing.dedup_key,
        path=str(out_path),
    )


# ── helpers ────────────────────────────────────────────────────────────


def _expand_domain_tags(domain_tags: list[str]) -> set[str]:
    """Build the set of profile tags relevant to this job's domain.

    Starts with the raw analysis domain_tags, expands synonyms via
    ``_TAG_EXPANSION``, and adds universally-relevant tags so that
    leadership and general-technical bullets are never dropped.
    """
    tags = {t.lower() for t in domain_tags}
    expanded = set(tags)
    for tag in tags:
        expanded.update(_TAG_EXPANSION.get(tag, []))
    expanded.update(_ALWAYS_RELEVANT_TAGS)
    return expanded


def _positioning_rules(domain_tags: list[str]) -> str:
    """Select positioning guidance based on job domain tags."""
    tags = {t.lower() for t in domain_tags}
    rules: list[str] = []

    if tags & {"defense", "military", "government", "aerospace"}:
        rules.append("- For defense roles: lead with clearance, Lincoln Lab, Electric Boat.")
    if tags & {"tech", "software", "engineering", "saas"}:
        rules.append("- For tech roles: lead with technical depth, startup, MIT Sloan.")
    if tags & {"energy", "nuclear", "clean", "climate"}:
        rules.append("- For energy roles: lead with nuclear coursework, systems engineering.")
    if tags & {"quant", "finance", "trading", "fintech"}:
        rules.append("- For quant roles: lead with math, probability, FinTech ML.")
    if tags & {"ai", "ml", "data", "machine learning"}:
        rules.append("- For AI/ML roles: lead with GenAI Lab, Spyglass pipeline, ML coursework.")
    if tags & {"startup", "product", "growth"}:
        rules.append("- For startup/product roles: lead with Gauntlet-42, MIT Co-ops, MBA.")

    if not rules:
        rules.append("- Position the candidate's strongest and most relevant experience first.")

    return "\n".join(rules)


def _safe_filename(company: str, title: str, dedup_key: str) -> str:
    """Build a filesystem-safe filename from listing fields."""
    raw = f"{company}_{title}".replace(" ", "_")
    safe = "".join(c for c in raw if c.isalnum() or c in ("_", "-"))
    return f"{safe[:50]}_{dedup_key[:8]}"
