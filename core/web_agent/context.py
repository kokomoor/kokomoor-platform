"""LLM context window management for the web agent.

As the agent runs through many observe-act cycles, the full history
quickly exceeds LLM context limits. This module compresses older steps
into short summaries while keeping the most recent steps verbatim.

Token budget allocation:
- System prompt + goal: always included (fixed cost)
- Current page state: always included (variable, ~500-1000 tokens)
- Recent steps: last ``keep_recent`` steps kept verbatim
- Older steps: compressed to one-line summaries
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from core.web_agent.protocol import AgentGoal, AgentStep

logger = structlog.get_logger(__name__)


def _summarize_step(step: AgentStep) -> str:
    """Compress one step into a single line for history."""
    action = step.action_taken
    verb = action.action
    ok = "ok" if step.result.success else "FAILED"

    if verb in ("fill", "type_text"):
        val = (action.value or "")[:40]
        return f"Step {step.step_number}: {verb} [{action.element_index}] '{val}' → {ok}"
    if verb in ("click", "check", "select"):
        val = f" '{action.value}'" if action.value else ""
        return f"Step {step.step_number}: {verb} [{action.element_index}]{val} → {ok}"
    if verb == "navigate":
        return f"Step {step.step_number}: navigate '{action.value}' → {ok}"
    if verb == "scroll":
        return f"Step {step.step_number}: scroll {action.value or 'down'} → {ok}"
    if verb == "press_key":
        return f"Step {step.step_number}: press '{action.value}' → {ok}"
    return f"Step {step.step_number}: {verb} → {ok}"


class AgentContextManager:
    """Build prompt context from agent goal, page state, and history.

    Keeps the last ``keep_recent`` steps verbatim and compresses earlier
    steps into one-line summaries to fit within the token budget.
    """

    def __init__(
        self,
        *,
        goal: AgentGoal,
        system_prompt: str = "",
        keep_recent: int = 5,
        max_summary_lines: int = 20,
    ) -> None:
        self._goal = goal
        self._system_prompt = system_prompt
        self._keep_recent = keep_recent
        self._max_summary_lines = max_summary_lines

    def build_prompt(
        self,
        page_state_text: str,
        history: list[AgentStep],
    ) -> str:
        """Assemble the full user prompt for the LLM.

        Returns a single string containing the goal, compressed history,
        current page state, and instructions for the next action.
        """
        parts: list[str] = []

        parts.append(f"## Goal\n{self._goal.instruction}")

        if self._goal.success_signals:
            parts.append("Success signals: " + ", ".join(self._goal.success_signals))
        if self._goal.failure_signals:
            parts.append("Failure signals: " + ", ".join(self._goal.failure_signals))
        if self._goal.require_human_approval_before:
            parts.append(
                "PAUSE for human approval before: "
                + ", ".join(self._goal.require_human_approval_before)
            )

        if history:
            parts.append(self._format_history(history))

        parts.append(f"## Current Page State\n{page_state_text}")

        parts.append(
            "## Your Task\n"
            "Decide the single best next action. Respond with JSON matching "
            "the AgentAction schema."
        )

        return "\n\n".join(parts)

    def build_system(self) -> str:
        """Return the system prompt (fixed across turns)."""
        base = (
            "You are a web automation agent. You observe structured page state "
            "and decide one action at a time to accomplish the stated goal. "
            "Always explain your reasoning. Use element indices from the page state. "
            "If you are done, use action='done'. If you are stuck, use action='stuck'."
        )
        if self._system_prompt:
            return f"{base}\n\n{self._system_prompt}"
        return base

    def _format_history(self, history: list[AgentStep]) -> str:
        lines: list[str] = ["## Action History"]

        split = max(0, len(history) - self._keep_recent)
        older = history[:split]
        recent = history[split:]

        if older:
            summaries = [_summarize_step(s) for s in older]
            if len(summaries) > self._max_summary_lines:
                kept = summaries[-(self._max_summary_lines):]
                lines.append(f"(… {len(summaries) - len(kept)} earlier steps omitted …)")
                summaries = kept
            lines.extend(summaries)

        if recent:
            if older:
                lines.append("--- recent (verbatim) ---")
            for step in recent:
                lines.append(
                    f"Step {step.step_number} @ {step.page_url}\n"
                    f"  Action: {step.action_taken.action} "
                    f"element=[{step.action_taken.element_index}] "
                    f"value='{step.action_taken.value or ''}'\n"
                    f"  Reasoning: {step.action_taken.reasoning}\n"
                    f"  Result: {'success' if step.result.success else 'FAILED: ' + step.result.error}\n"
                    f"  Page summary: {step.page_state_summary}"
                )

        return "\n".join(lines)
