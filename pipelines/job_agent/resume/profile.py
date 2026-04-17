"""Master resume profile loader and LLM formatter.

Loads the structured YAML profile and produces an LLM-readable
representation with visible bullet IDs for the tailoring plan pass.

Schema v2 surface conventions in the LLM view:
  - [exp_id] HEADER LINE ends with a tier marker: (PINNED) or (OPTIONAL).
  - Bullets whose ``anchor`` flag is True are prefixed with ``(ANCHOR)``.
  - A bullet's ``source_material`` (when present) is inlined as a
    ``source:`` block so the LLM has a rich palette of facts to draw
    from when issuing a ``recast`` op.
  - Supplementary projects appear as their own section at the bottom of
    the view.

Tier-aware filtering:
  - Pinned sections are ALWAYS visible with ALL bullets (the LLM picks
    which to render; the applier auto-inserts forgotten anchored bullets).
  - Optional sections appear only if at least one bullet's tags intersect
    ``relevant_tags``; within such a section, only tag-matching bullets
    plus anchored bullets are shown.
  - ``relevant_tags=None`` disables filtering entirely (full profile view).
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 — used at runtime
from typing import Any

import structlog
import yaml

from pipelines.job_agent.models.resume_tailoring import (
    MasterBullet,
    MasterEducation,
    MasterExperience,
    ResumeMasterProfile,
    SupplementaryProject,
)

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
        supplementary_projects=len(profile.supplementary_projects),
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
        relevant_tags: If provided, drives optional-section visibility —
            an optional section appears only if any of its bullets has a
            tag in this set. Pinned sections always appear regardless.
            Passing ``None`` disables filtering (full profile).
    """
    lines: list[str] = []

    lines.append(f"Name: {profile.name} | Location: {profile.location}")
    if profile.clearance:
        lines.append(f"Clearance: {profile.clearance}")
    lines.append("")

    lines.append("EXPERIENCE:")
    for exp in profile.experience:
        if exp.tier == "supplementary":
            # Supplementary content never appears in Experience; rendered
            # via the dedicated PROJECTS block below.
            continue
        bullets_to_show = _select_visible_bullets(exp.bullets, exp.tier, relevant_tags)
        if not bullets_to_show:
            # Pinned sections always show at least their anchored bullets.
            # If relevant_tags filtered everything out of an optional
            # section, skip it.
            if exp.tier == "pinned":
                bullets_to_show = exp.bullets
            else:
                continue
        _append_experience_header(lines, exp)
        for b in bullets_to_show:
            _append_bullet(lines, b)
        lines.append("")

    lines.append("EDUCATION:")
    for edu in profile.education:
        bullets_to_show = _select_visible_bullets(edu.bullets, edu.tier, relevant_tags)
        if not bullets_to_show:
            if edu.tier == "pinned":
                bullets_to_show = edu.bullets
            else:
                continue
        _append_education_header(lines, edu)
        for b in bullets_to_show:
            _append_bullet(lines, b)
        lines.append("")

    if profile.supplementary_projects:
        lines.append("SUPPLEMENTARY PROJECTS (rendered under Additional Information):")
        for proj in profile.supplementary_projects:
            _append_supplementary_project(lines, proj)
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


# ── internal helpers ──────────────────────────────────────────────────


def _select_visible_bullets(
    bullets: list[MasterBullet],
    tier: str,
    relevant_tags: set[str] | None,
) -> list[MasterBullet]:
    """Return bullets visible to the LLM for this section.

    - ``relevant_tags=None`` → all bullets visible (unfiltered full profile).
    - pinned section → all bullets visible (LLM picks; applier enforces anchors).
    - optional section → bullets matching tags, plus anchored bullets.
    """
    if relevant_tags is None:
        return list(bullets)
    if tier == "pinned":
        return list(bullets)
    matched = [b for b in bullets if b.anchor or (set(b.tags) & relevant_tags)]
    return matched


def _append_experience_header(lines: list[str], exp: MasterExperience) -> None:
    tier_label = f"({exp.tier.upper()})"
    header = f"[{exp.id}] {exp.company} | {exp.title} | {exp.dates} {tier_label}"
    lines.append(header)
    if exp.subtitle:
        lines.append(f"  subtitle: {exp.subtitle}")


def _append_education_header(lines: list[str], edu: MasterEducation) -> None:
    tier_label = f"({edu.tier.upper()})"
    gpa = f" | GPA: {edu.gpa}" if edu.gpa else ""
    header = f"[{edu.id}] {edu.school} | {edu.degree} | {edu.graduation}{gpa} {tier_label}"
    lines.append(header)


def _append_bullet(lines: list[str], b: MasterBullet) -> None:
    tag_str = ", ".join(b.tags) if b.tags else "general"
    anchor_marker = " (ANCHOR)" if b.anchor else ""
    lines.append(f"  - [{b.id}]{anchor_marker} {b.text} [tags: {tag_str}]")
    if b.variants.get("short"):
        lines.append(f"    short variant: {b.variants['short']}")
    if b.source_material:
        source = b.source_material.strip()
        lines.append("    source_material (fact palette for recast):")
        for src_line in source.splitlines():
            stripped = src_line.strip()
            if stripped:
                lines.append(f"      {stripped}")


def _append_supplementary_project(lines: list[str], proj: SupplementaryProject) -> None:
    tag_str = ", ".join(proj.tags) if proj.tags else "general"
    url_str = f" | url: {proj.url}" if proj.url else ""
    lines.append(f"  - [{proj.id}] {proj.name}{url_str} [tags: {tag_str}]")
    lines.append(f"    text: {proj.text}")
    if proj.variants.get("short"):
        lines.append(f"    short variant: {proj.variants['short']}")
    if proj.source_material:
        source = proj.source_material.strip()
        lines.append("    source_material (fact palette for recast):")
        for src_line in source.splitlines():
            stripped = src_line.strip()
            if stripped:
                lines.append(f"      {stripped}")
