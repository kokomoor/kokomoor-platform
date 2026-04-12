"""Shared models for the application engine.

Holds per-attempt result state (:class:`ApplicationAttempt`) that every
submitter — API, template, browser agent — returns so the orchestrating
node, tracking sink, and metrics layer can all speak the same type.

The candidate-profile models live in
``pipelines.job_agent.models.application``; keeping those separate from
this file avoids a circular import between ``models/`` and
``application/``.
"""

from __future__ import annotations

from typing import Literal

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
