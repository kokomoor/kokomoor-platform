"""Prompt builder for structured cover-letter plans."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pipelines.job_agent.models.resume_tailoring import JobAnalysisResult


def build_cover_letter_prompt(
    *,
    template: str,
    job_title: str,
    company: str,
    job_description: str,
    job_analysis: JobAnalysisResult,
    inventory_view: str,
    positioning_rules: str = "",
) -> str:
    """Build the per-listing user prompt from the variable-content template.

    The static portion (objectives, style guide, tone rules, hard
    requirements) lives in the cached system prompt composed by the
    cover-letter node — it does not appear here.
    """
    return template.format(
        job_title=job_title,
        company=company,
        job_description=job_description,
        job_analysis=job_analysis.model_dump_json(indent=2),
        candidate_inventory=inventory_view,
        positioning_rules=positioning_rules,
    )


def build_cover_letter_system(*, system_template: str, style_guide: str) -> str:
    """Render the static cover-letter system prompt once per run.

    Must be byte-identical across every call within the run for the
    Anthropic prefix cache to hit. Do not embed timestamps or per-item
    data here.
    """
    return system_template.format(style_guide=style_guide)
