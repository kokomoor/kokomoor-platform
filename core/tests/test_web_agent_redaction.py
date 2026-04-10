from __future__ import annotations

from core.browser.actions import ActionResult
from core.web_agent.context import AgentContextManager
from core.web_agent.protocol import AgentAction, AgentGoal, AgentStep


def test_sensitive_values_redacted_in_history_prompt() -> None:
    AgentStep.model_rebuild(_types_namespace={"ActionResult": ActionResult})
    goal = AgentGoal(instruction="Fill out login form")
    ctx = AgentContextManager(goal=goal, keep_recent=5)
    step = AgentStep(
        step_number=1,
        page_url="https://example.com/login",
        action_taken=AgentAction(
            reasoning="fill password",
            action="fill",
            element_index=2,
            value="super-secret-password",
        ),
        result=ActionResult(success=True),
        page_state_summary="Login page",
    )
    prompt = ctx.build_prompt("state", [step])
    assert "super-secret-password" not in prompt
    assert "<redacted>" in prompt


def test_type_text_values_redacted_in_history_prompt() -> None:
    """type_text payloads are always redacted in verbose history."""
    AgentStep.model_rebuild(_types_namespace={"ActionResult": ActionResult})
    goal = AgentGoal(instruction="Type credentials")
    ctx = AgentContextManager(goal=goal, keep_recent=5)

    action = AgentAction(
        reasoning="type password",
        action="type_text",
        element_index=3,
        value="my-secret-pw",
        confidence=1.0,
    )
    step = AgentStep.model_construct(
        step_number=1,
        page_url="https://example.com/login",
        action_taken=action,
        result=ActionResult(success=True),
        page_state_summary="Login page",
    )
    prompt = ctx.build_prompt("state", [step])
    assert "my-secret-pw" not in prompt
    assert "<redacted>" in prompt


def test_type_text_redacted_in_summary_path() -> None:
    """Verify the compressed summary path also redacts type_text values."""
    AgentStep.model_rebuild(_types_namespace={"ActionResult": ActionResult})
    goal = AgentGoal(instruction="Type credentials")
    ctx = AgentContextManager(goal=goal, keep_recent=0)

    action = AgentAction(
        reasoning="type secret",
        action="type_text",
        element_index=1,
        value="classified-data",
        confidence=1.0,
    )
    step = AgentStep.model_construct(
        step_number=1,
        page_url="https://example.com",
        action_taken=action,
        result=ActionResult(success=True),
        page_state_summary="Page",
    )
    prompt = ctx.build_prompt("state", [step])
    assert "classified-data" not in prompt
    assert "<redacted>" in prompt
