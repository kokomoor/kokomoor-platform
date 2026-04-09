"""Deterministic mapping from validated plan to render document."""

from __future__ import annotations

from pipelines.job_agent.cover_letter.models import CoverLetterDocument, CoverLetterPlan


def apply_cover_letter_plan(plan: CoverLetterPlan) -> CoverLetterDocument:
    """Build deterministic renderer input from validated plan data."""
    return CoverLetterDocument(
        salutation=plan.salutation,
        opening_paragraph=plan.opening_paragraph,
        body_paragraphs=plan.body_paragraphs,
        closing_paragraph=plan.closing_paragraph,
        signoff=plan.signoff,
        signature_name=plan.signature_name,
    )
