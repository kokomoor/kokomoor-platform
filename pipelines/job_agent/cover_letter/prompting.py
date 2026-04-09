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
    style_guide: str,
) -> str:
    """Build one-pass prompt from template + style/context data."""
    return template.format(
        job_title=job_title,
        company=company,
        job_description=job_description,
        job_analysis=job_analysis.model_dump_json(indent=2),
        candidate_inventory=inventory_view,
        style_guide=style_guide,
    )
