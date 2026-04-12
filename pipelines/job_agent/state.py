"""LangGraph state schema for the job application pipeline.

Defines the typed state that flows through the graph. Each node reads
from and writes to this state, and LangGraph manages persistence and
checkpointing between nodes.

LangGraph compiles dataclass state into a ``TypedDict``-shaped return
value, so ``coerce_state`` is provided to rehydrate either form back into
a real ``JobAgentState`` instance for callers that want attribute access.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import StrEnum
from typing import Any

from pipelines.job_agent.models import JobListing, SearchCriteria
from pipelines.job_agent.models.resume_tailoring import JobAnalysisResult  # noqa: TC001


class PipelinePhase(StrEnum):
    """Current phase of the pipeline run."""

    DISCOVERY = "discovery"
    FILTERING = "filtering"
    BULK_EXTRACTION = "bulk_extraction"
    JOB_ANALYSIS = "job_analysis"
    RANKING = "ranking"
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

    Note: human-review and application phases are not yet implemented;
    when they land they should add their own fields here rather than
    re-using ``tailored_listings`` so the lifecycle stays explicit.
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

    # Tailoring → Tracking
    tailored_listings: list[JobListing] = field(default_factory=list)

    # Error tracking
    errors: list[dict[str, str]] = field(default_factory=list)

    # Run metadata
    run_id: str = ""
    dry_run: bool = False


def coerce_state(value: Any) -> JobAgentState:
    """Coerce a LangGraph result into a ``JobAgentState`` instance.

    LangGraph's ``CompiledStateGraph.ainvoke`` returns a ``dict`` whose
    keys map to the dataclass field names. Direct attribute access on
    that dict raises ``AttributeError`` (the bug that crashed the test
    run). This helper normalises both shapes into a real dataclass.

    Unknown keys are dropped to keep the call resilient against
    LangGraph adding internal bookkeeping fields in the future.
    """
    if isinstance(value, JobAgentState):
        return value
    if isinstance(value, dict):
        known = {f.name for f in fields(JobAgentState)}
        kwargs = {k: v for k, v in value.items() if k in known}
        return JobAgentState(**kwargs)
    msg = f"Cannot coerce {type(value).__name__} to JobAgentState"
    raise TypeError(msg)
