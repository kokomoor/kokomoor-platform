"""Shared base models for all pipelines.

Provides common mixins and base classes that pipeline-specific models
inherit from. Ensures consistent patterns for timestamps, status
tracking, and soft deletion across the entire platform.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Optional
from uuid import uuid4

from sqlmodel import Field, SQLModel


class TimestampMixin(SQLModel):
    """Mixin that adds created_at and updated_at fields."""

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class BaseModel(TimestampMixin):
    """Base for all database-persisted models.

    Uses UUID strings as primary keys for portability across
    SQLite and Postgres without integer-sequence headaches.
    """

    id: str = Field(
        default_factory=lambda: uuid4().hex,
        primary_key=True,
        max_length=32,
    )


class PipelineRunStatus(str, Enum):
    """Universal status enum for pipeline run tracking."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PipelineRun(BaseModel, table=True):
    """Tracks individual pipeline execution runs across all pipelines.

    Every pipeline (job_agent, ml_showcase, etc.) logs its runs here
    so there is a single place to see what ran, when, and whether it
    succeeded.
    """

    __tablename__ = "pipeline_runs"

    pipeline_name: str = Field(index=True, max_length=64)
    status: PipelineRunStatus = Field(default=PipelineRunStatus.PENDING)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None
    metadata_json: Optional[str] = Field(
        default=None,
        description="JSON-serialised run metadata (node counts, durations, etc.)",
    )
