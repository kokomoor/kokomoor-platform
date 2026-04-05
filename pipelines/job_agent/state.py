"""LangGraph state schema for the job application pipeline.

Defines the typed state that flows through the graph. Each node reads
from and writes to this state, and LangGraph manages persistence and
checkpointing between nodes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from pipelines.job_agent.models import JobListing, SearchCriteria


class PipelinePhase(StrEnum):
    """Current phase of the pipeline run."""

    DISCOVERY = "discovery"
    FILTERING = "filtering"
    TAILORING = "tailoring"
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

    # Pipeline progress
    phase: PipelinePhase = PipelinePhase.DISCOVERY

    # Discovery → Filtering
    discovered_listings: list[JobListing] = field(default_factory=list)

    # Filtering → Tailoring
    qualified_listings: list[JobListing] = field(default_factory=list)

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
