"""LLM-driven web navigation via observe → decide → act loop.

The ``WebAgentController`` is the main entry point. It takes a Playwright
page, an LLM client, a declared goal, and repeatedly:

1. **Observes** the page via ``PageObserver`` → ``PageState``
2. **Decides** the next action via ``structured_complete`` → ``AgentAction``
3. **Executes** the action via ``BrowserActions`` → ``ActionResult``
4. Records the step and checks termination conditions.

The controller never auto-submits anything listed in
``goal.require_human_approval_before``. Instead it returns an
``AgentResult(status="awaiting_approval")`` so the caller can prompt for
human confirmation before proceeding.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import structlog

from core.browser.actions import ActionResult, BrowserActions
from core.browser.human_behavior import HumanBehavior
from core.browser.observer import PageObserver
from core.llm.structured import structured_complete
from core.web_agent.context import AgentContextManager
from core.web_agent.protocol import (
    AgentAction,
    AgentGoal,
    AgentResult,
    AgentStep,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from playwright.async_api import Page

    from core.llm.protocol import LLMClient

logger = structlog.get_logger(__name__)

_SENSITIVE_ACTIONS = {"fill", "type_text", "upload"}


class WebAgentController:
    """LLM-driven web navigation via observe → decide → act loop."""

    def __init__(
        self,
        page: Page,
        llm_client: LLMClient,
        *,
        goal: AgentGoal,
        actions: BrowserActions | None = None,
        observer: PageObserver | None = None,
        system_prompt: str = "",
        context_provider: Callable[..., Any] | None = None,
        run_id: str = "",
    ) -> None:
        self._page = page
        self._llm = llm_client
        self._goal = goal
        self._actions = actions or BrowserActions(page, HumanBehavior())
        self._observer = observer or PageObserver()
        self._context_provider = context_provider
        self._run_id = run_id or uuid.uuid4().hex[:12]
        self._ctx = AgentContextManager(goal=goal, system_prompt=system_prompt)
        self._history: list[AgentStep] = []

    async def run(self) -> AgentResult:
        """Execute the observe-act loop until goal is met or max_steps."""
        for step_num in range(1, self._goal.max_steps + 1):
            state = await self._observer.get_state(self._page)
            prompt = self._ctx.build_prompt(state.to_prompt(), self._history)

            try:
                action = await structured_complete(
                    self._llm,
                    prompt,
                    response_model=AgentAction,
                    run_id=self._run_id,
                    max_retries=1,
                )
            except ValueError as exc:
                logger.error("agent.llm_parse_failed", step=step_num, error=str(exc)[:200])
                return AgentResult(
                    status="error",
                    steps_taken=step_num,
                    final_url=self._page.url,
                    summary=f"LLM output parse failure: {exc}",
                    history=self._history,
                )

            logger.info(
                "agent.action_decided",
                step=step_num,
                action=action.action,
                element=action.element_index,
                value="<redacted>"
                if action.action in _SENSITIVE_ACTIONS
                else (action.value or "")[:60],
                confidence=action.confidence,
            )

            if action.action in self._goal.require_human_approval_before:
                return AgentResult(
                    status="awaiting_approval",
                    steps_taken=step_num,
                    final_url=self._page.url,
                    summary=f"Paused before '{action.action}' — awaiting human approval.",
                    last_action=action,
                    history=self._history,
                )

            if action.action == "done":
                return AgentResult(
                    status="completed",
                    steps_taken=step_num,
                    final_url=self._page.url,
                    summary=action.reasoning,
                    last_action=action,
                    history=self._history,
                )

            if action.action == "stuck":
                return AgentResult(
                    status="stuck",
                    steps_taken=step_num,
                    final_url=self._page.url,
                    summary=action.reasoning,
                    last_action=action,
                    history=self._history,
                )

            result = await self._execute(action)

            page_summary = f"{state.title} | {len(state.forms)} forms, {len(state.interactive_elements)} elements"
            self._history.append(
                AgentStep(
                    step_number=step_num,
                    page_url=self._page.url,
                    action_taken=action,
                    result=result,
                    page_state_summary=page_summary,
                )
            )

            if not result.success:
                logger.warning(
                    "agent.action_failed",
                    step=step_num,
                    action=action.action,
                    error=result.error,
                )

            if self._check_signals(state):
                return AgentResult(
                    status="completed",
                    steps_taken=step_num,
                    final_url=self._page.url,
                    summary="Success signal detected on page.",
                    last_action=action,
                    history=self._history,
                )

        return AgentResult(
            status="max_steps_reached",
            steps_taken=self._goal.max_steps,
            final_url=self._page.url,
            summary=f"Reached {self._goal.max_steps} steps without completing the goal.",
            history=self._history,
        )

    async def resume(self, *, approved: bool = True) -> AgentResult:
        """Resume after a human-approval pause.

        Call with ``approved=True`` to continue execution. The pending
        action (stored as ``last_action`` in the prior ``AgentResult``)
        should have been reviewed by a human.
        """
        if not approved:
            return AgentResult(
                status="stuck",
                steps_taken=len(self._history),
                final_url=self._page.url,
                summary="Human declined the pending action.",
                history=self._history,
            )
        return await self.run()

    # ------------------------------------------------------------------
    # Action dispatch
    # ------------------------------------------------------------------

    async def _execute(self, action: AgentAction) -> ActionResult:
        """Map an ``AgentAction`` to a ``BrowserActions`` call."""
        try:
            if action.action == "click":
                return await self._execute_click(action)
            if action.action == "fill":
                return await self._execute_fill(action)
            if action.action == "select":
                return await self._execute_select(action)
            if action.action == "check":
                return await self._execute_check(action)
            if action.action == "scroll":
                return await self._actions.scroll(
                    direction=action.value or "down",
                    amount=500,
                )
            if action.action == "navigate":
                nav = await self._actions.goto(action.value or "")
                return ActionResult(success=nav.success, error=nav.error)
            if action.action == "wait":
                if action.value:
                    ok = await self._actions.wait_for(action.value, timeout_ms=5_000)
                    return ActionResult(success=ok, error="" if ok else "Wait timed out")
                import asyncio

                await asyncio.sleep(2)
                return ActionResult(success=True)
            if action.action == "press_key":
                return await self._actions.press_key(action.value or "Enter")
            if action.action == "upload":
                return await self._execute_upload(action)

            return ActionResult(success=False, error=f"Unknown action: {action.action}")
        except Exception as exc:
            return ActionResult(success=False, error=str(exc)[:300])

    async def _execute_click(self, action: AgentAction) -> ActionResult:
        if action.element_index is not None:
            el = await self._observer.get_element_by_index(action.element_index)
            if el is not None:
                try:
                    await self._actions._behavior.human_click(self._actions.page, el)
                    await self._actions._behavior.between_actions_pause()
                    return ActionResult(success=True)
                except Exception as exc:
                    return ActionResult(success=False, error=str(exc)[:300])
        if action.value:
            return await self._actions.click(action.value)
        return ActionResult(success=False, error="No element_index or selector provided for click")

    async def _execute_fill(self, action: AgentAction) -> ActionResult:
        if action.element_index is not None:
            el = await self._observer.get_element_by_index(action.element_index)
            if el is not None:
                try:
                    await self._actions._behavior.human_click(self._actions.page, el)
                    await el.evaluate("el => el.value = ''")
                    await self._actions._behavior.type_with_cadence(el, action.value or "")
                    await self._actions._behavior.between_actions_pause()
                    return ActionResult(success=True)
                except Exception as exc:
                    return ActionResult(success=False, error=str(exc)[:300])
        if action.value:
            return await self._actions.fill("input, textarea", action.value)
        return ActionResult(success=False, error="No element_index or value for fill")

    async def _execute_select(self, action: AgentAction) -> ActionResult:
        if action.element_index is not None:
            el = await self._observer.get_element_by_index(action.element_index)
            if el is not None:
                selector = await el.evaluate("el => el.id ? '#' + el.id : ''")
                if selector:
                    return await self._actions.select_option(selector, action.value or "")
        return ActionResult(success=False, error="Cannot resolve select element")

    async def _execute_check(self, action: AgentAction) -> ActionResult:
        if action.element_index is not None:
            el = await self._observer.get_element_by_index(action.element_index)
            if el is not None:
                try:
                    is_checked = await el.is_checked()
                    if not is_checked:
                        await self._actions._behavior.human_click(self._actions.page, el)
                    await self._actions._behavior.between_actions_pause()
                    return ActionResult(success=True)
                except Exception as exc:
                    return ActionResult(success=False, error=str(exc)[:300])
        return ActionResult(success=False, error="No element_index for check")

    async def _execute_upload(self, action: AgentAction) -> ActionResult:
        if action.element_index is not None and action.value:
            el = await self._observer.get_element_by_index(action.element_index)
            if el is not None:
                try:
                    await el.set_input_files(action.value)
                    await self._actions._behavior.between_actions_pause()
                    return ActionResult(success=True)
                except Exception as exc:
                    return ActionResult(success=False, error=str(exc)[:300])
        return ActionResult(success=False, error="No element_index or file path for upload")

    # ------------------------------------------------------------------
    # Signal detection
    # ------------------------------------------------------------------

    def _check_signals(self, state: Any) -> bool:
        """Check if any success/failure signals are present on the page."""
        if not self._goal.success_signals:
            return False

        haystack = f"{state.title} {state.visible_text}".lower()
        for signal in self._goal.success_signals:
            if signal.lower() in haystack:
                logger.info("agent.success_signal_detected", signal=signal)
                return True
        return False
