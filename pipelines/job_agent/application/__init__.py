"""Job application automation package."""

from pipelines.job_agent.application.router import (
    RouteDecision,
    SubmissionStrategy,
    detect_ats_platform,
    route_application,
)

# Import all submitters to ensure they register themselves with the registry
import pipelines.job_agent.application.submitters.greenhouse_api  # noqa: F401
import pipelines.job_agent.application.submitters.lever_api  # noqa: F401
import pipelines.job_agent.application.templates.ashby  # noqa: F401
import pipelines.job_agent.application.templates.linkedin_easy_apply  # noqa: F401
import pipelines.job_agent.application.agent_filler  # noqa: F401

__all__ = [
    "RouteDecision",
    "SubmissionStrategy",
    "detect_ats_platform",
    "route_application",
]
