"""Job agent data models.

Defines the core data types for the job application pipeline:
job listings, applications, search criteria, and tailored documents.
All models use Pydantic for validation and SQLModel for persistence.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003
from enum import StrEnum

from pydantic import BaseModel as PydanticBaseModel
from pydantic import Field as PydanticField
from sqlmodel import Field, SQLModel

from core.models import TimestampMixin

# ---------- Enums ----------


class ApplicationStatus(StrEnum):
    """Lifecycle states for a job application."""

    DISCOVERED = "discovered"
    FILTERED_OUT = "filtered_out"
    ANALYZING = "analyzing"
    TAILORING = "tailoring"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    APPLYING = "applying"
    APPLIED = "applied"
    SKIPPED = "skipped"
    ERRORED = "errored"
    INTERVIEW = "interview"
    OFFER = "offer"
    REJECTED = "rejected"


class JobSource(StrEnum):
    """Where a job listing was discovered."""

    LINKEDIN = "linkedin"
    WELLFOUND = "wellfound"
    BUILTIN = "builtin"
    LEVELS_FYI = "levels_fyi"
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    WORKDAY = "workday"
    COMPANY_SITE = "company_site"
    OTHER = "other"


# ---------- Database Models ----------


class JobListing(TimestampMixin, SQLModel, table=True):
    """A discovered job listing."""

    __tablename__ = "job_listings"

    id: int | None = Field(default=None, primary_key=True)
    title: str = Field(index=True, max_length=256)
    company: str = Field(index=True, max_length=256)
    location: str = Field(default="", max_length=256)
    url: str = Field(max_length=2048)
    source: JobSource = Field(default=JobSource.OTHER)
    description: str = Field(default="")
    salary_min: int | None = None
    salary_max: int | None = None
    remote: bool | None = None
    status: ApplicationStatus = Field(default=ApplicationStatus.DISCOVERED)
    dedup_key: str = Field(
        index=True,
        unique=True,
        max_length=512,
        description="Hash of (company, title, url) for deduplication.",
    )

    # Populated after tailoring.
    tailored_resume_path: str | None = None
    tailored_cover_letter_path: str | None = None

    # Populated after application.
    applied_at: datetime | None = None
    notes: str | None = None


# ---------- Pydantic Models (non-persisted) ----------


class SearchCriteria(PydanticBaseModel):
    """Input parameters for the Discovery node."""

    keywords: list[str] = PydanticField(default_factory=list)
    target_companies: list[str] = PydanticField(default_factory=list)
    target_roles: list[str] = PydanticField(default_factory=list)
    locations: list[str] = PydanticField(default_factory=list)
    salary_floor: int = 170_000
    remote_ok: bool = True
    sources: list[JobSource] = PydanticField(
        default_factory=lambda: [JobSource.LINKEDIN, JobSource.WELLFOUND, JobSource.BUILTIN]
    )


class JobFilter(PydanticBaseModel):
    """Criteria for filtering discovered listings."""

    salary_floor: int = 170_000
    exclude_contract: bool = True
    required_keywords: list[str] = PydanticField(default_factory=list)
    excluded_keywords: list[str] = PydanticField(default_factory=list)
