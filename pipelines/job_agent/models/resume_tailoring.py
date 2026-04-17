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
    """A single resume bullet with stable ID for LLM referencing.

    Schema v2 adds:
      anchor: bool — if True, this bullet is load-bearing for its section.
        The applier auto-prepends it to the section's rendered bullet list
        even if the LLM's plan did not select it, guaranteeing it appears.
      source_material: str — verbose prose (facts, scope, stack, outcomes)
        consumed only by the `recast` op. The tailoring LLM may compose
        new bullet text using only facts present in source_material or
        `text`, preserving numbers / proper nouns / technology names
        verbatim. Applier enforces length parity and entity grounding.
    """

    id: str
    text: str
    tags: list[str] = Field(default_factory=list)
    variants: dict[str, str] = Field(default_factory=dict)
    anchor: bool = False
    source_material: str = ""


class MasterExperience(BaseModel):
    """An experience entry in the master resume profile.

    Schema v2 adds:
      tier: "pinned" | "optional" | "supplementary"
        * pinned — section MUST appear in every tailored resume; applier
          auto-inserts it if the LLM forgot.
        * optional — section MAY appear when at least one bullet has a
          tag matching the job's domain tags.
        * supplementary — never rendered in the main experience block;
          effectively a marker (supplementary content belongs in the
          top-level supplementary_projects list).
      Default is "pinned" for backward compatibility: v1 profiles without
      explicit tier treat every section as must-include, preserving the
      user's full work history by default.
    """

    id: str
    company: str
    title: str
    dates: str = ""
    location: str = ""
    subtitle: str = ""
    tier: Literal["pinned", "optional", "supplementary"] = "pinned"
    bullets: list[MasterBullet]


class MasterEducation(BaseModel):
    """An education entry in the master resume profile.

    Education sections default to `tier="pinned"` like experience: they
    are always-include unless explicitly marked optional.
    """

    id: str
    school: str
    degree: str
    graduation: str
    gpa: str = ""
    location: str = ""
    tier: Literal["pinned", "optional"] = "pinned"
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


class CoverLetterPreferences(BaseModel):
    """Optional profile knobs for cover-letter voice and constraints."""

    preferred_tone: str = ""
    preferred_signoff: str = ""
    positioning_angles: list[str] = Field(default_factory=list)
    motivation_themes: list[str] = Field(default_factory=list)
    target_industries: list[str] = Field(default_factory=list)
    emphasize_topics: list[str] = Field(default_factory=list)
    de_emphasize_topics: list[str] = Field(default_factory=list)
    hard_constraints: list[str] = Field(default_factory=list)
    style_preferences: list[str] = Field(default_factory=list)
    banned_phrases: list[str] = Field(default_factory=list)
    narrative_themes: list[str] = Field(default_factory=list)


class SupplementaryProject(BaseModel):
    """A personal project rendered under 'Additional Information'.

    These are not work experience — they exist separately so personal
    projects (side projects, OSS contributions, portfolio pieces) can be
    surfaced without competing for experience-section real estate. The
    tailoring plan may include them or omit them per-role via
    ``ResumeTailoringPlan.supplementary_project_ids``.
    """

    id: str
    name: str
    url: str = ""
    text: str
    tags: list[str] = Field(default_factory=list)
    variants: dict[str, str] = Field(default_factory=dict)
    source_material: str = ""


class ResumeMasterProfile(BaseModel):
    """Complete master resume profile.

    Contains *all* possible experience bullets with stable IDs,
    tags for domain filtering, and optional length variants.
    Loaded once per tailoring run from the candidate_profile YAML.

    Schema v2 adds ``supplementary_projects`` (personal projects rendered
    in the Additional Information section, not in Experience).
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
    supplementary_projects: list[SupplementaryProject] = Field(default_factory=list)
    cover_letter: CoverLetterPreferences | None = None

    def get_supplementary_project(self, project_id: str) -> SupplementaryProject | None:
        """Look up a supplementary project by ID."""
        for p in self.supplementary_projects:
            if p.id == project_id:
                return p
        return None

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


# ── LLM Output: Job Analysis ──────────────────────────────────────────


class JobAnalysisResult(BaseModel):
    """Structured extraction from a job description.

    Produced by the job-analysis node (dedicated LangGraph node).
    Analyses the full JD without referencing the candidate profile.
    Consumed by the tailoring node for plan generation.
    """

    themes: list[str]
    seniority: str
    domain_tags: list[str]
    must_hit_keywords: list[str]
    priority_requirements: list[str]
    basic_qualifications: list[str] = Field(default_factory=list)
    preferred_qualifications: list[str] = Field(default_factory=list)
    angles: list[str]


# ── LLM Output: Tailoring Plan (Pass 2) ───────────────────────────────


class BulletOp(BaseModel):
    """Operation to apply to a single resume bullet.

    Ops:
      - keep: use the master bullet's text verbatim.
      - shorten: use the ``short`` variant if defined, else fall back to text.
      - recast: produce new text using only facts present in the bullet's
        master ``text`` or ``source_material``. All numbers, dollar
        amounts, percentages, and proper nouns must appear verbatim.
        Word count must not exceed the master's by more than 20%. The
        applier validates both constraints and falls back to ``keep`` on
        failure.
      - rewrite: deprecated alias for recast, retained for backward
        compatibility with older plans. Treated identically to recast.
    """

    bullet_id: str
    op: Literal["keep", "shorten", "recast", "rewrite"]
    rewrite_text: str | None = None


class SectionPlan(BaseModel):
    """Bullet selection and ordering for one resume section."""

    section_id: str
    bullet_order: list[str]


class ResumeTailoringPlan(BaseModel):
    """Complete tailoring plan produced by the second LLM pass.

    References master-profile bullet IDs so the applier can
    deterministically assemble the tailored resume.

    Schema v2 adds ``supplementary_project_ids`` — the subset of
    supplementary projects the LLM has chosen to surface in this tailored
    resume's Additional Information section.
    """

    summary: str
    experience_sections: list[SectionPlan]
    education_sections: list[SectionPlan]
    bullet_ops: list[BulletOp]
    skills_to_highlight: list[str]
    supplementary_project_ids: list[str] = Field(default_factory=list)


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


class TailoredSupplementaryProject(BaseModel):
    """A supplementary project entry in the tailored resume.

    Rendered as a single line under Additional Information / Projects.
    """

    id: str
    name: str
    url: str = ""
    text: str


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
    supplementary_projects: list[TailoredSupplementaryProject] = Field(default_factory=list)
