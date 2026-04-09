"""LangGraph state schema for the job application pipeline.

Defines the typed state that flows through the graph. Each node reads
from and writes to this state, and LangGraph manages persistence and
checkpointing between nodes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from pipelines.job_agent.models import JobListing, SearchCriteria
from pipelines.job_agent.models.resume_tailoring import JobAnalysisResult  # noqa: TC001


class PipelinePhase(StrEnum):
    """Current phase of the pipeline run."""

    DISCOVERY = "discovery"
    FILTERING = "filtering"
    JOB_ANALYSIS = "job_analysis"
    TAILORING = "tailoring"
    COVER_LETTER_TAILORING = "cover_letter_tailoring"
    HUMAN_REVIEW = "human_review"
    APPLICATION = "application"
    TRACKING = "tracking"
    NOTIFICATION = "notification"
    COMPLETE = "complete"
    ERROR = "error"


@dataclass
class JobAgentState:
    """State object passed through the LangGraph pipeline.

    LangGraph nodes receive this state, modify relevant fields, and
    return it. The state captures everything needed to resume from
    any checkpoint.
    """

    # Input
    search_criteria: SearchCriteria = field(default_factory=SearchCriteria)
    manual_job_url: str = ""

    # Pipeline progress
    phase: PipelinePhase = PipelinePhase.DISCOVERY

    # Discovery → Filtering
    discovered_listings: list[JobListing] = field(default_factory=list)

    # Filtering → Job Analysis
    qualified_listings: list[JobListing] = field(default_factory=list)

    # Job Analysis → Tailoring (keyed by listing dedup_key)
    job_analyses: dict[str, JobAnalysisResult] = field(default_factory=dict)
    # Internal cache keyed by dedup_key + description hash.
    job_analysis_cache: dict[str, JobAnalysisResult] = field(default_factory=dict)

    # Tailoring → Human Review
    tailored_listings: list[JobListing] = field(default_factory=list)

    # Human Review → Application
    approved_listings: list[JobListing] = field(default_factory=list)
    rejected_listing_ids: list[int] = field(default_factory=list)

    # Application → Tracking
    applied_listings: list[JobListing] = field(default_factory=list)

    # Error tracking
    errors: list[dict[str, str]] = field(default_factory=list)

    # Run metadata
    run_id: str = ""
    dry_run: bool = False
