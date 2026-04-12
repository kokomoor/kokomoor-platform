"""Pydantic models for the web-agent action vocabulary.

These models define the contract between the LLM and the browser action
layer. The LLM receives a ``PageState`` (from ``core.browser.observer``)
and returns an ``AgentAction``. The controller executes the action and
records the outcome as an ``AgentStep``.

Keeping these models in ``core/`` (not in a pipeline) allows any
pipeline to build on the same agent loop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from core.browser.actions import ActionResult


class AgentAction(BaseModel):
    """Single action the LLM wants to take on the current page."""

    reasoning: str = Field(
        description="Chain-of-thought explaining the decision (logged, not shown to user)."
    )
    action: Literal[
        "click",
        "fill",
        "type_text",
        "select",
        "check",
        "scroll",
        "navigate",
        "wait",
        "press_key",
        "upload",
        "done",
        "stuck",
    ] = Field(description="The action verb to execute.")
    element_index: int | None = Field(
        default=None,
        description="Index of the target element from the PageState snapshot.",
    )
    value: str | None = Field(
        default=None,
        description="Text to type, URL to navigate to, key to press, file path, etc.",
    )
    confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Self-assessed confidence in this action (0-1).",
    )


class AgentStep(BaseModel):
    """One completed observe-act cycle in the agent history."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    step_number: int
    page_url: str
    action_taken: AgentAction
    result: ActionResult
    page_state_summary: str = Field(
        description="Compressed one-line summary of the page state for history."
    )


class AgentGoal(BaseModel):
    """Declarative specification of what the agent should accomplish."""

    instruction: str = Field(
        description="Natural language instruction, e.g. 'Fill out this job application'."
    )
    success_signals: list[str] = Field(
        default_factory=list,
        description="Phrases/patterns that indicate the goal is complete.",
    )
    failure_signals: list[str] = Field(
        default_factory=list,
        description="Phrases/patterns that indicate an unrecoverable failure.",
    )
    max_steps: int = Field(
        default=30,
        description="Hard limit on observe-act cycles before giving up.",
    )
    require_human_approval_before: list[str] = Field(
        default_factory=list,
        description="Action types that must pause for human approval (e.g. 'submit').",
    )


class AgentResult(BaseModel):
    """Final outcome of a web-agent run."""

    status: Literal[
        "completed",
        "stuck",
        "max_steps_reached",
        "awaiting_approval",
        "error",
    ]
    steps_taken: int = 0
    final_url: str = ""
    summary: str = ""
    last_action: AgentAction | None = None
    history: list[AgentStep] = Field(default_factory=list)
