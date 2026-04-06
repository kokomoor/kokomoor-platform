"""Pydantic models for the multi-phase resume tailoring pipeline.

Three model categories:
1. Master profile — loaded from YAML, contains all possible resume content with stable IDs.
2. LLM outputs — structured results from job analysis and tailoring plan passes.
3. Tailored document — post-application representation ready for .docx rendering.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# ── Master Profile (loaded from YAML) ──────────────────────────────────


class MasterBullet(BaseModel):
    """A single resume bullet with stable ID for LLM referencing."""

    id: str
    text: str
    tags: list[str] = Field(default_factory=list)
    variants: dict[str, str] = Field(default_factory=dict)


class MasterExperience(BaseModel):
    """An experience entry in the master resume profile."""

    id: str
    company: str
    title: str
    dates: str = ""
    location: str = ""
    subtitle: str = ""
    bullets: list[MasterBullet]


class MasterEducation(BaseModel):
    """An education entry in the master resume profile."""

    id: str
    school: str
    degree: str
    graduation: str
    gpa: str = ""
    location: str = ""
    bullets: list[MasterBullet] = Field(default_factory=list)


class MasterSkills(BaseModel):
    """Skills groupings in the master profile."""

    languages: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)

    def all_skills(self) -> list[str]:
        """Flat list of every skill across all categories."""
        return self.languages + self.frameworks + self.domains + self.tools


class ResumeMasterProfile(BaseModel):
    """Complete master resume profile.

    Contains *all* possible experience bullets with stable IDs,
    tags for domain filtering, and optional length variants.
    Loaded once per tailoring run from the candidate_profile YAML.
    """

    schema_version: int = 1
    name: str
    location: str = ""
    email: str = ""
    phone: str = ""
    linkedin: str = ""
    github: str = ""
    clearance: str = ""
    education: list[MasterEducation]
    experience: list[MasterExperience]
    skills: MasterSkills

    def get_bullet(self, bullet_id: str) -> MasterBullet | None:
        """Look up a bullet by ID across all experience and education sections."""
        for exp in self.experience:
            for b in exp.bullets:
                if b.id == bullet_id:
                    return b
        for edu in self.education:
            for b in edu.bullets:
                if b.id == bullet_id:
                    return b
        return None

    def all_bullet_ids(self) -> set[str]:
        """Return every bullet ID in the profile for plan validation."""
        ids: set[str] = set()
        for exp in self.experience:
            for b in exp.bullets:
                ids.add(b.id)
        for edu in self.education:
            for b in edu.bullets:
                ids.add(b.id)
        return ids

    def get_experience(self, section_id: str) -> MasterExperience | None:
        """Look up an experience entry by its stable ID."""
        for exp in self.experience:
            if exp.id == section_id:
                return exp
        return None

    def get_education(self, section_id: str) -> MasterEducation | None:
        """Look up an education entry by its stable ID."""
        for edu in self.education:
            if edu.id == section_id:
                return edu
        return None


# ── LLM Output: Job Analysis (Pass 1) ─────────────────────────────────


class JobAnalysisResult(BaseModel):
    """Structured extraction from a job description.

    Produced by the first LLM pass — purely analyses the JD
    without referencing the candidate profile.
    """

    themes: list[str]
    seniority: str
    domain_tags: list[str]
    must_hit_keywords: list[str]
    priority_requirements: list[str]
    angles: list[str]


# ── LLM Output: Tailoring Plan (Pass 2) ───────────────────────────────


class BulletOp(BaseModel):
    """Operation to apply to a single resume bullet."""

    bullet_id: str
    op: Literal["keep", "shorten", "rewrite"]
    rewrite_text: str | None = None


class SectionPlan(BaseModel):
    """Bullet selection and ordering for one resume section."""

    section_id: str
    bullet_order: list[str]


class ResumeTailoringPlan(BaseModel):
    """Complete tailoring plan produced by the second LLM pass.

    References master-profile bullet IDs so the applier can
    deterministically assemble the tailored resume.
    """

    summary: str
    experience_sections: list[SectionPlan]
    education_sections: list[SectionPlan]
    bullet_ops: list[BulletOp]
    skills_to_highlight: list[str]


# ── Tailored Document (post-application) ──────────────────────────────


class TailoredBullet(BaseModel):
    """A bullet in the final tailored resume."""

    id: str
    text: str


class TailoredExperience(BaseModel):
    """An experience entry in the tailored resume, ready for rendering."""

    company: str
    title: str
    dates: str
    location: str = ""
    subtitle: str = ""
    bullets: list[TailoredBullet]


class TailoredEducation(BaseModel):
    """An education entry in the tailored resume, ready for rendering."""

    school: str
    degree: str
    graduation: str
    gpa: str
    location: str = ""
    bullets: list[TailoredBullet]


class TailoredResumeDocument(BaseModel):
    """Final structured resume — input to the .docx renderer."""

    name: str
    location: str
    email: str
    phone: str
    linkedin: str
    github: str
    clearance: str
    summary: str
    experience: list[TailoredExperience]
    education: list[TailoredEducation]
    skills_highlight: list[str]
    additional_info: list[str] = Field(default_factory=list)
