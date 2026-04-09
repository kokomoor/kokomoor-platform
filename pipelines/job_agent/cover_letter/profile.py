"""Cover-letter profile helpers: inventory formatting and style-guide loading."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from pipelines.job_agent.models.resume_tailoring import (
        MasterBullet,
        ResumeMasterProfile,
    )

logger = structlog.get_logger(__name__)


_DEFAULT_STYLE_GUIDE = """# Cover Letter Style Guide

Write in a confident, respectful business voice. Be specific and concrete.
Use one introduction paragraph, one to two body paragraphs, and one closing paragraph.
Ground claims in real candidate evidence. Avoid generic filler and unsupported claims.
No em dashes or en dashes in prose.
"""


def format_cover_letter_inventory(
    profile: ResumeMasterProfile,
    *,
    relevant_tags: set[str] | None = None,
) -> str:
    """Render concise profile evidence with stable IDs for reference checks.

    Args:
        profile: The loaded master resume profile.
        relevant_tags: If provided, only include bullets whose tags overlap
            with this set. Sections with no remaining bullets are omitted.
            Passing ``None`` includes everything (full profile).
    """
    lines: list[str] = []

    lines.append(f"Candidate: {profile.name}")
    if profile.location:
        lines.append(f"Location: {profile.location}")
    if profile.clearance:
        lines.append(f"Clearance: {profile.clearance}")
    lines.append("")

    if profile.cover_letter is not None:
        prefs = profile.cover_letter
        lines.append("COVER LETTER PREFERENCES:")
        if prefs.preferred_tone:
            lines.append(f"- Preferred tone: {prefs.preferred_tone}")
        if prefs.preferred_signoff:
            lines.append(f"- Preferred signoff: {prefs.preferred_signoff}")
        _append_list(lines, "Positioning angles", prefs.positioning_angles)
        _append_list(lines, "Motivation themes", prefs.motivation_themes)
        _append_list(lines, "Target industries", prefs.target_industries)
        _append_list(lines, "Emphasize topics", prefs.emphasize_topics)
        _append_list(lines, "De-emphasize topics", prefs.de_emphasize_topics)
        _append_list(lines, "Hard constraints", prefs.hard_constraints)
        _append_list(lines, "Style preferences", prefs.style_preferences)
        _append_list(lines, "Banned phrases", prefs.banned_phrases)
        _append_list(lines, "Narrative themes", prefs.narrative_themes)
        lines.append("")

    lines.append("EXPERIENCE EVIDENCE:")
    for exp in profile.experience:
        filtered = _filter_bullets(exp.bullets, relevant_tags)
        if not filtered and relevant_tags is not None:
            continue
        lines.append(f"[{exp.id}] {exp.company} | {exp.title} | {exp.dates}")
        for bullet in filtered:
            lines.append(f"  - [{bullet.id}] {bullet.text}")
    lines.append("")

    lines.append("EDUCATION EVIDENCE:")
    for edu in profile.education:
        filtered = _filter_bullets(edu.bullets, relevant_tags)
        if not filtered and relevant_tags is not None:
            continue
        lines.append(f"[{edu.id}] {edu.school} | {edu.degree} | {edu.graduation}")
        for bullet in filtered:
            lines.append(f"  - [{bullet.id}] {bullet.text}")

    return "\n".join(lines)


def load_cover_letter_style_guide(path: Path) -> str:
    """Load local markdown style guide; fallback to default if missing/empty."""
    if not path.exists():
        logger.warning("cover_letter.style_guide_missing", path=str(path))
        return _DEFAULT_STYLE_GUIDE

    content = path.read_text(encoding="utf-8").strip()
    if not content:
        logger.warning("cover_letter.style_guide_empty", path=str(path))
        return _DEFAULT_STYLE_GUIDE

    return content


def _filter_bullets(
    bullets: list[MasterBullet],
    relevant_tags: set[str] | None,
) -> list[MasterBullet]:
    """Return bullets matching *relevant_tags*, or all if tags is None."""
    if relevant_tags is None:
        return bullets
    return [b for b in bullets if set(b.tags) & relevant_tags]


def _append_list(lines: list[str], label: str, values: list[str]) -> None:
    if values:
        lines.append(f"- {label}: {', '.join(values)}")
