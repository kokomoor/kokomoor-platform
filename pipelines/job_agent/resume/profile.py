"""Master resume profile loader and LLM formatter.

Loads the structured YAML profile and produces an LLM-readable
representation with visible bullet IDs for the tailoring plan pass.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 — used at runtime
from typing import Any

import structlog
import yaml

from pipelines.job_agent.models.resume_tailoring import ResumeMasterProfile

logger = structlog.get_logger(__name__)


def load_master_profile(path: Path) -> ResumeMasterProfile:
    """Load and validate the master resume profile from a YAML file.

    Raises:
        FileNotFoundError: If the profile YAML does not exist.
        ValueError: If the YAML does not match the expected schema.
    """
    if not path.exists():
        msg = f"Master profile not found: {path}"
        raise FileNotFoundError(msg)

    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        msg = f"Expected YAML mapping, got {type(raw).__name__}"
        raise ValueError(msg)

    profile = ResumeMasterProfile.model_validate(raw)
    logger.info(
        "master_profile_loaded",
        path=str(path),
        experience_sections=len(profile.experience),
        education_sections=len(profile.education),
        total_bullets=len(profile.all_bullet_ids()),
    )
    return profile


def format_profile_for_llm(profile: ResumeMasterProfile) -> str:
    """Render the master profile as structured text with visible bullet IDs.

    The LLM uses the bracketed IDs (e.g. ``[ll_radar]``) to reference
    specific bullets in its tailoring plan.
    """
    lines: list[str] = []

    lines.append(f"Name: {profile.name} | Location: {profile.location}")
    if profile.clearance:
        lines.append(f"Clearance: {profile.clearance}")
    lines.append("")

    lines.append("EXPERIENCE:")
    for exp in profile.experience:
        lines.append(f"[{exp.id}] {exp.company} | {exp.title} | {exp.dates}")
        for b in exp.bullets:
            tag_str = ", ".join(b.tags) if b.tags else "general"
            lines.append(f"  - [{b.id}] {b.text} [{tag_str}]")
        lines.append("")

    lines.append("EDUCATION:")
    for edu in profile.education:
        gpa = f" | GPA: {edu.gpa}" if edu.gpa else ""
        lines.append(f"[{edu.id}] {edu.school} | {edu.degree} | {edu.graduation}{gpa}")
        for b in edu.bullets:
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
