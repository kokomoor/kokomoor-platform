"""Job application automation package."""

from pipelines.job_agent.application.router import (
    RouteDecision,
    SubmissionStrategy,
    detect_ats_platform,
    route_application,
)

__all__ = [
    "RouteDecision",
    "SubmissionStrategy",
    "detect_ats_platform",
    "route_application",
]
