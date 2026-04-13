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

import asyncio
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

_SENSITIVE_ACTIONS = {"fill", "type_text", "upload", "press_key"}


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
        frame_interest_patterns: list[str] | None = None,
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
        self._frame_interest_patterns = frame_interest_patterns

    async def run(self) -> AgentResult:
        """Execute the observe-act loop until goal is met or max_steps."""
        for step_num in range(1, self._goal.max_steps + 1):
            state = await self._observer.get_state(
                self._page, interest_patterns=self._frame_interest_patterns
            )
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
            if action.action == "type_text":
                return await self._execute_type_text(action)
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

    async def _execute_type_text(self, action: AgentAction) -> ActionResult:
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
            return await self._actions.type_text("input, textarea", action.value)
        return ActionResult(success=False, error="No element_index or value for type_text")

    async def _execute_select(self, action: AgentAction) -> ActionResult:
        """Handle both native <select> and custom dropdown widgets."""
        if action.element_index is None:
            return ActionResult(success=False, error="No element_index for select")

        el = await self._observer.get_element_by_index(action.element_index)
        if el is None:
            return ActionResult(success=False, error="Element not found")

        tag = await el.evaluate("el => el.tagName.toLowerCase()")

        # 1. Native <select>: use Playwright's select_option
        if tag == "select":
            try:
                # Try by label first, then value
                try:
                    await el.select_option(label=action.value)
                except Exception:
                    await el.select_option(value=action.value)
                await self._actions._behavior.between_actions_pause()
                return ActionResult(success=True)
            except Exception as exc:
                return ActionResult(success=False, error=str(exc)[:300])

        # 2. Custom dropdown: click trigger → wait for listbox → click option
        try:
            logger.debug("agent.executing_custom_select", element=action.element_index)
            await self._actions._behavior.human_click(self._page, el)

            # Combined selector for performance - avoid sequential 2s waits
            listbox_selector = (
                "[role='listbox'], [role='option'], .select-menu, "
                "[data-automation-id*='selectWidget'], .dropdown-menu"
            )

            try:
                await self._page.wait_for_selector(listbox_selector, timeout=2500)
            except Exception:
                return ActionResult(success=False, error="Dropdown options did not appear")

            # Find and click the matching option
            options = await self._page.query_selector_all("[role='option'], .option, .dropdown-item")
            target = (action.value or "").lower()
            for opt in options:
                text = (await opt.text_content() or "").strip()
                if target in text.lower() or text.lower() in target:
                    await self._actions._behavior.human_click(self._page, opt)
                    await self._actions._behavior.between_actions_pause()
                    return ActionResult(success=True)

            return ActionResult(
                success=False,
                error=f"Option '{action.value}' not found in dropdown",
            )
        except Exception as exc:
            return ActionResult(success=False, error=str(exc)[:300])

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
        """Robust file upload with multiple fallback strategies."""
        file_path = action.value
        if not file_path:
            return ActionResult(success=False, error="No file path for upload")

        # Strategy 1: Direct set_input_files on indexed element
        if action.element_index is not None:
            el = await self._observer.get_element_by_index(action.element_index)
            if el:
                try:
                    await el.set_input_files(file_path)
                    await self._actions._behavior.between_actions_pause()
                    logger.debug("agent.upload_strategy_1_success")
                    return ActionResult(success=True)
                except Exception as exc:
                    logger.debug("agent.upload_strategy_1_failed", error=str(exc))

        # Strategy 2: Find any file input on the page (even hidden)
        try:
            file_input = await self._page.query_selector("input[type='file']")
            if file_input:
                await file_input.set_input_files(file_path)
                await self._actions._behavior.between_actions_pause()
                logger.debug("agent.upload_strategy_2_success")
                return ActionResult(success=True)
        except Exception as exc:
            logger.debug("agent.upload_strategy_2_failed", error=str(exc))

        # Strategy 3: Find file input in iframes
        for frame in self._page.frames:
            if frame == self._page.main_frame:
                continue
            try:
                file_input = await frame.query_selector("input[type='file']")
                if file_input:
                    await file_input.set_input_files(file_path)
                    await self._actions._behavior.between_actions_pause()
                    logger.debug("agent.upload_strategy_3_success", frame=frame.url)
                    return ActionResult(success=True)
            except Exception:
                continue

        # Strategy 4: Click upload trigger and use file chooser
        upload_triggers = [
            "text=Upload", "text=Choose file", "text=Browse",
            "text=Attach", "[class*='upload']", "[class*='drop']",
        ]
        for trigger in upload_triggers:
            try:
                el = await self._page.query_selector(trigger)
                if el and await el.is_visible():
                    async with self._page.expect_file_chooser(timeout=3000) as fc:
                        await el.click()
                    chooser = await fc.value
                    await chooser.set_files(file_path)
                    await self._actions._behavior.between_actions_pause()
                    logger.debug("agent.upload_strategy_4_success", trigger=trigger)
                    return ActionResult(success=True)
            except Exception:
                continue

        return ActionResult(success=False, error="All upload strategies failed")

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
