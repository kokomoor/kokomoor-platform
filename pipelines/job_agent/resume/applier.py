"""Apply a tailoring plan to the master profile, producing a renderable document.

Pure function — no LLM calls, no I/O. Given a master profile and a
structured plan (bullet selections, ordering, rewrites), outputs the
final ``TailoredResumeDocument`` ready for .docx rendering.
"""

from __future__ import annotations

import structlog

from pipelines.job_agent.models.resume_tailoring import (
    BulletOp,
    MasterBullet,
    ResumeMasterProfile,
    ResumeTailoringPlan,
    TailoredBullet,
    TailoredEducation,
    TailoredExperience,
    TailoredResumeDocument,
)

logger = structlog.get_logger(__name__)

MAX_BULLETS_PER_SECTION = 5


def apply_tailoring_plan(
    profile: ResumeMasterProfile,
    plan: ResumeTailoringPlan,
) -> TailoredResumeDocument:
    """Apply *plan* to *profile* and return the tailored document.

    Unknown bullet or section IDs are logged and skipped rather than
    raising — the rest of the resume is still usable.
    """
    valid_ids = profile.all_bullet_ids()
    ops_by_id = {op.bullet_id: op for op in plan.bullet_ops}

    experience: list[TailoredExperience] = []
    for sec in plan.experience_sections:
        master_exp = profile.get_experience(sec.section_id)
        if master_exp is None:
            logger.warning("applier.unknown_section", section_id=sec.section_id)
            continue

        bullets = _resolve_bullets(master_exp.bullets, sec.bullet_order, ops_by_id, valid_ids)
        experience.append(
            TailoredExperience(
                company=master_exp.company,
                title=master_exp.title,
                dates=master_exp.dates,
                bullets=bullets[:MAX_BULLETS_PER_SECTION],
            )
        )

    education: list[TailoredEducation] = []
    for sec in plan.education_sections:
        master_edu = profile.get_education(sec.section_id)
        if master_edu is None:
            logger.warning("applier.unknown_section", section_id=sec.section_id)
            continue

        bullets = _resolve_bullets(master_edu.bullets, sec.bullet_order, ops_by_id, valid_ids)
        education.append(
            TailoredEducation(
                school=master_edu.school,
                degree=master_edu.degree,
                graduation=master_edu.graduation,
                gpa=master_edu.gpa,
                bullets=bullets,
            )
        )

    return TailoredResumeDocument(
        name=profile.name,
        location=profile.location,
        email=profile.email,
        phone=profile.phone,
        linkedin=profile.linkedin,
        github=profile.github,
        clearance=profile.clearance,
        summary=plan.summary,
        experience=experience,
        education=education,
        skills_highlight=plan.skills_to_highlight,
    )


# ── helpers ────────────────────────────────────────────────────────────


def _resolve_bullets(
    master_bullets: list[MasterBullet],
    ordered_ids: list[str],
    ops_by_id: dict[str, BulletOp],
    valid_ids: set[str],
) -> list[TailoredBullet]:
    """Select, order, and transform bullets according to the plan."""

    by_id = {b.id: b for b in master_bullets}
    result: list[TailoredBullet] = []

    for bid in ordered_ids:
        if bid not in valid_ids:
            logger.warning("applier.unknown_bullet", bullet_id=bid)
            continue
        master = by_id.get(bid)
        if master is None:
            logger.warning("applier.bullet_not_in_section", bullet_id=bid)
            continue

        op = ops_by_id.get(bid)
        text = _apply_op(master, op)
        result.append(TailoredBullet(id=bid, text=text))

    return result


def _apply_op(bullet: MasterBullet, op: BulletOp | None) -> str:
    """Return the final bullet text after applying the requested operation."""
    if op is None or op.op == "keep":
        return bullet.text

    if op.op == "shorten":
        short = bullet.variants.get("short")
        if short:
            return short
        logger.warning("applier.no_short_variant", bullet_id=bullet.id)
        return bullet.text

    if op.op == "rewrite" and op.rewrite_text:
        return op.rewrite_text

    return bullet.text
