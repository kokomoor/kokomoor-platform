"""Candidate application profile — flat data for job application forms.

This profile is a machine-readable representation of every answer a job
application form might ask for: name, email, phone, address, EEO
responses, education summary, work authorization, source tracking, etc.

It is **distinct from** ``ResumeMasterProfile`` in ``resume_tailoring.py``:
the master profile structures experience bullets for resume tailoring;
this profile is the flat form-field data consumed by the application
engine's deterministic field mapper, LLM question answerer, and API
submitters (Greenhouse, Lever).

Usage:

    from pathlib import Path
    from pipelines.job_agent.models import load_application_profile

    profile = load_application_profile(
        Path("pipelines/job_agent/context/candidate_application.yaml")
    )
    print(profile.personal.first_name)
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

ApplicationStatusLiteral = Literal["submitted", "awaiting_review", "stuck", "error"]


class ApplicationAttempt(BaseModel):
    """Result of one application submission attempt.

    Every submitter strategy (Greenhouse API, Lever API, LinkedIn Easy
    Apply template, Workday agent filler, etc.) produces exactly one
    :class:`ApplicationAttempt`. The orchestrator appends it to
    ``JobAgentState.application_results`` and the tracking sink persists
    it.

    Status semantics:

    - ``submitted`` — the form was posted and the ATS accepted it.
    - ``awaiting_review`` — the attempt completed up to the final submit
      click but paused for human review (dry-run, or
      ``application_require_human_review=True``).
    - ``stuck`` — the submitter could not make progress and gave up
      cleanly (rate-limited, unknown page layout, missing asset).
    - ``error`` — an unrecoverable failure such as a validation error
      from the ATS or an unexpected HTTP status.
    """

    model_config = ConfigDict(extra="forbid")

    dedup_key: str = Field(
        description="Dedup key of the listing this attempt belongs to.",
    )
    status: ApplicationStatusLiteral = Field(
        description="Terminal state of the attempt — see class docstring.",
    )
    strategy: str = Field(
        default="",
        description=(
            "Which submitter ran, e.g. 'api_greenhouse', 'api_lever', "
            "'template_linkedin', 'agent_workday'."
        ),
    )
    summary: str = Field(
        default="",
        description="One-line human-readable summary of what happened.",
    )
    steps_taken: int = Field(
        default=0,
        ge=0,
        description="Discrete steps taken (fields filled, pages visited, etc.).",
    )
    screenshot_path: str = Field(
        default="",
        description="Path to a final-state screenshot; empty for headless flows.",
    )
    errors: list[str] = Field(
        default_factory=list,
        description="Error messages encountered, if any.",
    )
    fields_filled: int = Field(
        default=0,
        ge=0,
        description="Number of form fields successfully filled.",
    )
    llm_calls_made: int = Field(
        default=0,
        ge=0,
        description="LLM calls consumed to answer custom questions.",
    )


class PersonalInfo(BaseModel):
    """Core personal identifiers — name, contact, profile links."""

    model_config = ConfigDict(extra="forbid")

    first_name: str = Field(description="Legal first name as it appears on ID.")
    last_name: str = Field(description="Legal last name / surname.")
    preferred_name: str = Field(
        default="",
        description="Preferred first name / nickname for forms that ask for one.",
    )
    email: str = Field(description="Primary contact email. Must not be empty.")
    phone: str = Field(
        description=(
            "E.164-style phone number, e.g. '+18603895347'. Used for forms that "
            "validate strict international format."
        ),
    )
    phone_formatted: str = Field(
        description=(
            "Human-readable phone number, e.g. '(860) 389-5347'. Used for forms "
            "that validate US national format."
        ),
    )
    linkedin_url: str = Field(default="", description="Full LinkedIn profile URL.")
    github_url: str = Field(default="", description="Full GitHub profile URL.")
    portfolio_url: str = Field(
        default="",
        description="Optional personal portfolio / writing / project site URL.",
    )
    website_url: str = Field(
        default="",
        description="Optional generic personal website URL.",
    )


class AddressInfo(BaseModel):
    """Mailing address used for location fields on applications."""

    model_config = ConfigDict(extra="forbid")

    street: str = Field(default="", description="Street address line 1.")
    city: str = Field(description="City / locality.")
    state: str = Field(
        description="US state code (e.g. 'MA') or full state name.",
    )
    zip: str = Field(default="", description="Postal / ZIP code.")
    country: str = Field(default="United States", description="Country name.")


class AuthorizationInfo(BaseModel):
    """Work authorization, sponsorship, citizenship, and clearance data."""

    model_config = ConfigDict(extra="forbid")

    authorized_us: bool = Field(
        description="True if the candidate is legally authorized to work in the US.",
    )
    require_sponsorship: bool = Field(
        description=("True if the candidate requires visa sponsorship now or in the future."),
    )
    citizenship: str = Field(
        default="",
        description="Citizenship status, e.g. 'US Citizen' or 'Permanent Resident'.",
    )
    clearance: str = Field(
        default="",
        description=(
            "Active security clearance string, e.g. 'DoD Final Secret (active)'. Empty if none."
        ),
    )


class DemographicInfo(BaseModel):
    """EEO / voluntary self-identification responses.

    All fields are US federal EEOC categories and are voluntary. Use
    'Decline to self-identify' or similar declining text to map to the
    decline option on forms that offer it.
    """

    model_config = ConfigDict(extra="forbid")

    gender: str = Field(description="Gender response as it should appear on forms.")
    race_ethnicity: str = Field(
        description=(
            "Race / ethnicity response (e.g. 'White', 'Two or more races', "
            "'Decline to self-identify')."
        ),
    )
    veteran_status: str = Field(
        description=("Veteran self-identification string (e.g. 'I am not a protected veteran')."),
    )
    disability_status: str = Field(
        description=("Disability self-identification string (e.g. 'I do not have a disability')."),
    )


class AdditionalDegree(BaseModel):
    """One prior degree. Used to fill repeating education sections."""

    model_config = ConfigDict(extra="forbid")

    degree: str = Field(description="Degree name, e.g. 'BS Computer Engineering'.")
    school: str = Field(description="Institution awarding the degree.")
    year: str = Field(description="Graduation year as a string, e.g. '2021'.")


class EducationInfo(BaseModel):
    """Highest-degree summary plus any prior degrees."""

    model_config = ConfigDict(extra="forbid")

    highest_degree: str = Field(
        description="Short name of the highest degree (e.g. 'MBA', 'BS', 'PhD').",
    )
    school: str = Field(description="Institution awarding the highest degree.")
    graduation_year: str = Field(
        description="Graduation year for the highest degree, as a string.",
    )
    gpa: str = Field(default="", description="GPA as a string. Empty if not reporting.")
    field_of_study: str = Field(
        description="Field of study / major for the highest degree.",
    )
    additional_degrees: list[AdditionalDegree] = Field(
        default_factory=list,
        description="Prior degrees to fill repeating education sections.",
    )


class LanguageProficiency(BaseModel):
    """One spoken language and the candidate's self-assessed proficiency."""

    model_config = ConfigDict(extra="forbid")

    language: str = Field(description="Language name, e.g. 'English'.")
    proficiency: str = Field(
        description="Proficiency level, e.g. 'Native', 'Fluent', 'Conversational'.",
    )


class ScreeningInfo(BaseModel):
    """Generic screening-question answers that don't vary per listing."""

    model_config = ConfigDict(extra="forbid")

    years_experience: str = Field(
        description=(
            "Years of relevant experience as a string. Many forms want it as text "
            "rather than a number."
        ),
    )
    willing_to_relocate: bool = Field(
        description="True if willing to relocate for the role.",
    )
    desired_salary: str = Field(
        description="Desired annual salary as a string, e.g. '200000'.",
    )
    earliest_start_date: str = Field(
        default="",
        description=("Earliest start date as ISO-ish text, or empty to leave blank on forms."),
    )
    how_did_you_hear: str = Field(
        description="Default answer for 'How did you hear about us?' fields.",
    )
    referral_name: str = Field(
        default="",
        description="Referrer name for forms that ask. Empty if none.",
    )
    languages_spoken: list[LanguageProficiency] = Field(
        default_factory=list,
        description="Languages and proficiency for forms that list them.",
    )


class SourceTracking(BaseModel):
    """Per-ATS override values for 'How did you hear about us?' fields."""

    model_config = ConfigDict(extra="forbid")

    default: str = Field(
        description="Default source answer when no per-ATS override applies.",
    )
    linkedin: str = Field(
        default="",
        description="Source answer for LinkedIn applications.",
    )
    greenhouse: str = Field(
        default="",
        description="Source answer for Greenhouse applications.",
    )
    lever: str = Field(
        default="",
        description="Source answer for Lever applications.",
    )
    indeed: str = Field(
        default="",
        description="Source answer for Indeed applications.",
    )


class CandidateApplicationProfile(BaseModel):
    """Flat representation of every candidate data point a job application
    form might ask for.

    Consumed by:

    - The deterministic field mapper (``application/field_mapper.py``).
    - The LLM question answerer (``application/qa_answerer.py``).
    - The API submitters (Greenhouse, Lever) for personal/contact fields.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(
        default=1,
        description="Profile schema version. Bump when adding required fields.",
    )
    personal: PersonalInfo = Field(description="Personal identifiers and profile links.")
    address: AddressInfo = Field(description="Mailing address.")
    authorization: AuthorizationInfo = Field(
        description="Work authorization, sponsorship, citizenship, clearance.",
    )
    demographics: DemographicInfo = Field(
        description="EEO voluntary self-identification responses.",
    )
    education: EducationInfo = Field(
        description="Highest degree plus any prior degrees.",
    )
    screening: ScreeningInfo = Field(
        description="Generic screening-question answers.",
    )
    source: SourceTracking = Field(
        description="Per-ATS source answers for 'How did you hear about us?'.",
    )


@lru_cache(maxsize=32)
def _load_cached(resolved_path: str) -> CandidateApplicationProfile:
    """Load and validate a profile. Cached on the resolved path string.

    Separated from :func:`load_application_profile` so callers can pass a
    ``Path`` without making the public signature cache-key-aware. Tests
    can clear state between cases via ``_load_cached.cache_clear()``.
    """
    path = Path(resolved_path)
    if not path.exists():
        msg = (
            f"Candidate application profile not found at {path}. "
            f"Copy candidate_application.example.yaml to candidate_application.yaml "
            f"and fill in the fields."
        )
        raise FileNotFoundError(msg)

    with path.open("r", encoding="utf-8") as f:
        raw: Any = yaml.safe_load(f)

    if not isinstance(raw, dict):
        msg = (
            f"Candidate application profile at {path} did not parse as a mapping "
            f"(got {type(raw).__name__}). Expected a top-level YAML dict."
        )
        raise ValueError(msg)

    return CandidateApplicationProfile.model_validate(raw)


def load_application_profile(path: Path) -> CandidateApplicationProfile:
    """Load a candidate application profile from YAML, with per-path caching.

    Successive calls for the same resolved path return the exact same
    instance (identity-preserving). Use ``_load_cached.cache_clear()`` in
    tests if you need to force a fresh load.

    Args:
        path: Path to the candidate application YAML file.

    Returns:
        A validated :class:`CandidateApplicationProfile`.

    Raises:
        FileNotFoundError: If the file does not exist.
        pydantic.ValidationError: If the YAML fails schema validation.
        ValueError: If the YAML does not parse as a mapping at the top level.
    """
    resolved = str(Path(path).resolve())
    return _load_cached(resolved)
