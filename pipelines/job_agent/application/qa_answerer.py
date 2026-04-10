"""QA answerer — maps form fields to candidate profile answers.

Given a candidate profile (YAML) and a form field description (label,
type, options), uses ``structured_complete`` to produce the correct
answer. The confidence score lets the form workflow decide whether to
proceed automatically or flag for human review.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, Field

from core.llm.structured import structured_complete

if TYPE_CHECKING:
    from core.llm.protocol import LLMClient

logger = structlog.get_logger(__name__)


class FormFieldAnswer(BaseModel):
    """LLM-generated answer for a single form field."""

    answer: str = Field(description="The value to fill into the field.")
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Self-assessed confidence that this is the correct answer.",
    )
    source: str = Field(
        description="Which section of the candidate profile this answer came from."
    )


_QA_SYSTEM = (
    "You are a job application assistant. Given a candidate profile and a form "
    "field from a job application, determine the correct answer. "
    "Use only information from the candidate profile. "
    "If the profile does not contain enough information to answer confidently, "
    "set confidence below 0.5 and note what is missing in the source field."
)


async def answer_form_field(
    llm: LLMClient,
    *,
    field_label: str,
    field_type: str,
    field_options: list[str] | None = None,
    candidate_profile: str,
    run_id: str = "",
) -> FormFieldAnswer:
    """Determine the correct answer for a form field from the candidate profile.

    Args:
        llm: The LLM client to use.
        field_label: The visible label of the form field (e.g. "Phone Number").
        field_type: The input type (e.g. "text", "email", "select", "radio").
        field_options: For select/radio fields, the available options.
        candidate_profile: The full candidate profile as YAML text.
        run_id: Pipeline run identifier for log correlation.

    Returns:
        A ``FormFieldAnswer`` with the answer, confidence, and source reference.
    """
    options_text = ""
    if field_options:
        options_text = f"\nAvailable options: {', '.join(field_options)}"

    prompt = (
        f"## Candidate Profile\n{candidate_profile}\n\n"
        f"## Form Field\n"
        f"Label: {field_label}\n"
        f"Type: {field_type}{options_text}\n\n"
        f"What should be entered in this field?"
    )

    return await structured_complete(
        llm,
        prompt,
        response_model=FormFieldAnswer,
        run_id=run_id,
    )
