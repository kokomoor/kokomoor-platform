"""Cover-letter planning and render contracts."""

from __future__ import annotations

from pydantic import BaseModel, Field


class RequirementEvidence(BaseModel):
    """Traceability mapping from job requirement to selected profile evidence."""

    requirement: str
    supporting_bullet_ids: list[str] = Field(min_length=1)


class CoverLetterPlan(BaseModel):
    """Structured LLM output for cover-letter generation."""

    salutation: str
    opening_paragraph: str
    body_paragraphs: list[str] = Field(min_length=1)
    closing_paragraph: str
    signoff: str
    signature_name: str
    company_motivation: str
    job_requirements_addressed: list[str] = Field(default_factory=list)
    selected_experience_ids: list[str] = Field(default_factory=list)
    selected_bullet_ids: list[str] = Field(default_factory=list)
    selected_education_ids: list[str] = Field(default_factory=list)
    requirement_evidence: list[RequirementEvidence] = Field(default_factory=list)
    tone_version: str = ""


class CoverLetterDocument(BaseModel):
    """Deterministic, validated cover-letter structure for renderer input."""

    salutation: str
    opening_paragraph: str
    body_paragraphs: list[str]
    closing_paragraph: str
    signoff: str
    signature_name: str

    def all_paragraphs(self) -> list[str]:
        return [self.opening_paragraph, *self.body_paragraphs, self.closing_paragraph]
