"""Master resume profile loader and LLM formatter.

Loads the structured YAML profile and produces an LLM-readable
representation with visible bullet IDs for the tailoring plan pass.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 — used at runtime
from typing import Any

import structlog
import yaml

from pipelines.job_agent.models.resume_tailoring import MasterBullet, ResumeMasterProfile

logger = structlog.get_logger(__name__)

# Per-process cache keyed by (resolved_path, mtime_ns). Each pipeline
# run touches the master profile from ranking, tailoring, and cover
# letter nodes; before this cache the YAML was parsed and validated 3x.
# Keying on mtime means a manual edit on disk still picks up cleanly
# without needing an explicit cache_clear() in tests.
_PROFILE_CACHE: dict[tuple[str, int], ResumeMasterProfile] = {}


def load_master_profile(path: Path) -> ResumeMasterProfile:
    """Load and validate the master resume profile from a YAML file.

    Result is cached by (path, mtime) so repeated calls within a run
    do not re-parse and re-validate the YAML.

    Raises:
        FileNotFoundError: If the profile YAML does not exist.
        ValueError: If the YAML does not match the expected schema.
    """
    if not path.exists():
        msg = f"Master profile not found: {path}"
        raise FileNotFoundError(msg)

    cache_key = (str(path.resolve()), path.stat().st_mtime_ns)
    cached = _PROFILE_CACHE.get(cache_key)
    if cached is not None:
        return cached

    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        msg = f"Expected YAML mapping, got {type(raw).__name__}"
        raise ValueError(msg)

    profile = ResumeMasterProfile.model_validate(raw)
    _PROFILE_CACHE[cache_key] = profile
    logger.info(
        "master_profile_loaded",
        path=str(path),
        experience_sections=len(profile.experience),
        education_sections=len(profile.education),
        total_bullets=len(profile.all_bullet_ids()),
    )
    return profile


def format_profile_for_llm(
    profile: ResumeMasterProfile,
    *,
    relevant_tags: set[str] | None = None,
) -> str:
    """Render the master profile as structured text with visible bullet IDs.

    The LLM uses the bracketed IDs (e.g. ``[ll_radar]``) to reference
    specific bullets in its tailoring plan.

    Args:
        profile: The loaded master resume profile.
        relevant_tags: If provided, only include bullets whose tags overlap
            with this set. Sections with no remaining bullets are omitted.
            Passing ``None`` includes everything (full profile).
    """
    lines: list[str] = []

    lines.append(f"Name: {profile.name} | Location: {profile.location}")
    if profile.clearance:
        lines.append(f"Clearance: {profile.clearance}")
    lines.append("")

    lines.append("EXPERIENCE:")
    for exp in profile.experience:
        filtered = _filter_bullets(exp.bullets, relevant_tags)
        if not filtered and relevant_tags is not None:
            continue
        lines.append(f"[{exp.id}] {exp.company} | {exp.title} | {exp.dates}")
        for b in filtered:
            tag_str = ", ".join(b.tags) if b.tags else "general"
            lines.append(f"  - [{b.id}] {b.text} [{tag_str}]")
        lines.append("")

    lines.append("EDUCATION:")
    for edu in profile.education:
        filtered = _filter_bullets(edu.bullets, relevant_tags)
        if not filtered and relevant_tags is not None:
            continue
        gpa = f" | GPA: {edu.gpa}" if edu.gpa else ""
        lines.append(f"[{edu.id}] {edu.school} | {edu.degree} | {edu.graduation}{gpa}")
        for b in filtered:
            tag_str = ", ".join(b.tags) if b.tags else "general"
            lines.append(f"  - [{b.id}] {b.text} [{tag_str}]")
        lines.append("")

    lines.append("SKILLS:")
    if profile.skills.languages:
        lines.append(f"  Languages: {', '.join(profile.skills.languages)}")
    if profile.skills.frameworks:
        lines.append(f"  Frameworks: {', '.join(profile.skills.frameworks)}")
    if profile.skills.domains:
        lines.append(f"  Domains: {', '.join(profile.skills.domains)}")
    if profile.skills.tools:
        lines.append(f"  Tools: {', '.join(profile.skills.tools)}")

    return "\n".join(lines)


def _filter_bullets(
    bullets: list[MasterBullet],
    relevant_tags: set[str] | None,
) -> list[MasterBullet]:
    """Return bullets matching *relevant_tags*, or all if tags is None."""
    if relevant_tags is None:
        return bullets
    return [b for b in bullets if set(b.tags) & relevant_tags]
