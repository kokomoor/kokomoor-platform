"""Submitter strategies for the application engine.

Each submitter is a single async function that takes a ``JobListing``
plus the shared application context (profile, resume, cover letter)
and returns an :class:`ApplicationAttempt`. The application router
picks one based on the listing URL / detected ATS and the node layer
records the result.
"""

from __future__ import annotations

from pipelines.job_agent.application.submitters.greenhouse_api import (
    submit_greenhouse_application,
)

__all__ = ["submit_greenhouse_application"]
