"""Cover-letter tailoring specialization modules."""

from pipelines.job_agent.cover_letter.applier import apply_cover_letter_plan
from pipelines.job_agent.cover_letter.models import CoverLetterDocument, CoverLetterPlan
from pipelines.job_agent.cover_letter.profile import (
    format_cover_letter_inventory,
    load_cover_letter_style_guide,
)
from pipelines.job_agent.cover_letter.renderer import render_cover_letter_docx
from pipelines.job_agent.cover_letter.validation import validate_cover_letter_plan

__all__ = [
    "CoverLetterDocument",
    "CoverLetterPlan",
    "apply_cover_letter_plan",
    "format_cover_letter_inventory",
    "load_cover_letter_style_guide",
    "render_cover_letter_docx",
    "validate_cover_letter_plan",
]
