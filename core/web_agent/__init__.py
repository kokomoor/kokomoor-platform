"""LLM-driven web agent — observe-decide-act loop.

This package provides the "brain" layer that sits on top of
``core.browser`` (the "hands and eyes"). It uses ``structured_complete``
to drive a Playwright page toward a declared goal.

Usage:
    from core.web_agent.protocol import AgentGoal
    from core.web_agent.controller import WebAgentController

    goal = AgentGoal(
        instruction="Fill out the job application form",
        success_signals=["application submitted", "confirmation"],
        failure_signals=["access denied", "error"],
        require_human_approval_before=["submit"],
    )
    result = await WebAgentController(page, llm, goal=goal).run()
"""

from core.web_agent.protocol import (
    AgentAction,
    AgentGoal,
    AgentResult,
    AgentStep,
)

__all__ = [
    "AgentAction",
    "AgentGoal",
    "AgentResult",
    "AgentStep",
]
