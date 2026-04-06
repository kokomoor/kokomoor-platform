"""Apply a tailoring plan to the master profile, producing a renderable document.

Pure function — no LLM calls, no I/O. Given a master profile and a
structured plan (bullet selections, ordering, rewrites), outputs the
final ``TailoredResumeDocument`` ready for .docx rendering.
"""

from __future__ import annotations

import re

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

MAX_EXPERIENCE_SECTIONS = 4
MAX_EDUCATION_SECTIONS = 2
MAX_BULLETS_PER_EXPERIENCE_SECTION = 4
MAX_BULLETS_PER_EDUCATION_SECTION = 2
MAX_SUMMARY_WORDS = 32
MAX_SKILLS_TO_HIGHLIGHT = 10


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
    for sec in plan.experience_sections[:MAX_EXPERIENCE_SECTIONS]:
        master_exp = profile.get_experience(sec.section_id)
        if master_exp is None:
            logger.warning("applier.unknown_section", section_id=sec.section_id)
            continue

        bullets = _resolve_bullets(master_exp.bullets, sec.bullet_order, ops_by_id, valid_ids)
        experience.append(
            TailoredExperience(
                company=_normalize_whitespace(master_exp.company),
                title=_normalize_whitespace(master_exp.title),
                dates=_normalize_whitespace(master_exp.dates),
                bullets=bullets[:MAX_BULLETS_PER_EXPERIENCE_SECTION],
            )
        )

    education: list[TailoredEducation] = []
    for sec in plan.education_sections[:MAX_EDUCATION_SECTIONS]:
        master_edu = profile.get_education(sec.section_id)
        if master_edu is None:
            logger.warning("applier.unknown_section", section_id=sec.section_id)
            continue

        bullets = _resolve_bullets(master_edu.bullets, sec.bullet_order, ops_by_id, valid_ids)
        education.append(
            TailoredEducation(
                school=_normalize_whitespace(master_edu.school),
                degree=_normalize_whitespace(master_edu.degree),
                graduation=_normalize_whitespace(master_edu.graduation),
                gpa=_normalize_whitespace(master_edu.gpa),
                bullets=bullets[:MAX_BULLETS_PER_EDUCATION_SECTION],
            )
        )

    return TailoredResumeDocument(
        name=_normalize_whitespace(profile.name),
        location=_normalize_whitespace(profile.location),
        email=_normalize_whitespace(profile.email),
        phone=_normalize_whitespace(profile.phone),
        linkedin=_normalize_whitespace(profile.linkedin),
        github=_normalize_whitespace(profile.github),
        clearance=_normalize_whitespace(profile.clearance),
        summary=_truncate_words(_normalize_inline_prose(plan.summary), MAX_SUMMARY_WORDS),
        experience=experience,
        education=education,
        skills_highlight=[
            _normalize_whitespace(skill)
            for skill in plan.skills_to_highlight[:MAX_SKILLS_TO_HIGHLIGHT]
        ],
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
        result.append(TailoredBullet(id=bid, text=_normalize_inline_prose(text)))

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


def _normalize_whitespace(text: str) -> str:
    """Normalize whitespace while preserving punctuation."""
    normalized = re.sub(r"\s+", " ", text).strip()
    return normalized


def _truncate_words(text: str, max_words: int) -> str:
    """Limit *text* to *max_words* words."""
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words])


def _normalize_inline_prose(text: str) -> str:
    """Normalize punctuation in narrative prose only.

    Rule: inline em/en dashes in sentence text are replaced with semicolons
    to avoid obvious LLM punctuation patterns. Date ranges and headings are
    preserved because they typically do not have surrounding spaces.
    """
    normalized = _normalize_whitespace(text)
    normalized = re.sub(r"\s[\u2014\u2013]\s", "; ", normalized)
    normalized = re.sub(r"\s--\s", "; ", normalized)
    return normalized
