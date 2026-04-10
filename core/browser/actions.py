"""Stealth-wrapped atomic browser interaction primitives.

Every method applies ``HumanBehavior`` automatically so callers never need
to manually insert pauses, mouse movements, or typing cadence. Both
programmatic discovery providers and the LLM-driven web-agent controller
use this same interface, ensuring uniform stealth characteristics.

Design rules
------------
- Every public method returns a typed result dataclass (never raises on
  routine failures like "element not found").
- The ``_resolve`` helper tries CSS → text-content → aria-label strategies
  so callers can pass a variety of selector styles.
- After each interaction a short ``between_actions_pause`` is applied.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import structlog

from core.browser.human_behavior import HumanBehavior

if TYPE_CHECKING:
    from playwright.async_api import ElementHandle, Page, Response

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ActionResult:
    """Outcome of an atomic browser action."""

    success: bool
    error: str = ""
    selector_found: bool = True
    extra: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class NavigationResult:
    """Outcome of a page navigation."""

    success: bool
    url: str = ""
    status: int | None = None
    error: str = ""


class BrowserActions:
    """Stealth-wrapped browser interaction primitives.

    Wraps Playwright page operations with ``HumanBehavior`` so that every
    click, type, and navigation looks human. Intended for both
    programmatic (provider) and LLM-driven (web agent) callers.
    """

    def __init__(self, page: Page, behavior: HumanBehavior | None = None) -> None:
        self._page = page
        self._behavior = behavior or HumanBehavior()

    @property
    def page(self) -> Page:
        return self._page

    # ------------------------------------------------------------------
    # Element resolution
    # ------------------------------------------------------------------

    async def _resolve(self, selector: str, *, index: int = 0) -> ElementHandle | None:
        """Find an element via CSS, then text, then aria-label fallback."""
        try:
            elements = await self._page.query_selector_all(selector)
            if elements and index < len(elements):
                return elements[index]
        except Exception:
            pass

        if not selector.startswith((".", "#", "[", "/", ":")):
            for strategy in [
                f"text={selector}",
                f'[aria-label="{selector}"]',
                f'[placeholder="{selector}"]',
                f'[name="{selector}"]',
            ]:
                try:
                    el = await self._page.query_selector(strategy)
                    if el is not None:
                        return el
                except Exception:
                    continue

        return None

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    async def goto(
        self,
        url: str,
        *,
        wait_until: Literal[
            "commit", "domcontentloaded", "load", "networkidle"
        ] = "domcontentloaded",
        timeout_ms: int = 30_000,
    ) -> NavigationResult:
        """Navigate to *url* with a human pause afterward."""
        try:
            resp: Response | None = await self._page.goto(
                url, wait_until=wait_until, timeout=timeout_ms
            )
            status = resp.status if resp else None
            logger.info("actions.goto", url=url, status=status)
            await self._behavior.between_actions_pause(min_s=0.5, max_s=1.5)
            return NavigationResult(success=True, url=self._page.url, status=status)
        except Exception as exc:
            logger.warning("actions.goto_failed", url=url, error=str(exc)[:200])
            return NavigationResult(success=False, url=url, error=str(exc)[:300])

    async def wait_for(
        self,
        selector: str,
        *,
        timeout_ms: int = 5_000,
        state: Literal["attached", "detached", "hidden", "visible"] = "visible",
    ) -> bool:
        """Wait for *selector* to reach *state*. Returns True on success."""
        try:
            await self._page.wait_for_selector(selector, timeout=timeout_ms, state=state)
            return True
        except Exception:
            return False

    async def reload(self) -> NavigationResult:
        """Reload the current page."""
        try:
            resp = await self._page.reload(wait_until="domcontentloaded")
            status = resp.status if resp else None
            await self._behavior.between_actions_pause(min_s=0.5, max_s=1.5)
            return NavigationResult(success=True, url=self._page.url, status=status)
        except Exception as exc:
            return NavigationResult(success=False, url=self._page.url, error=str(exc)[:300])

    # ------------------------------------------------------------------
    # Interaction primitives
    # ------------------------------------------------------------------

    async def click(self, selector: str, *, index: int = 0) -> ActionResult:
        """Click an element with natural mouse movement."""
        el = await self._resolve(selector, index=index)
        if el is None:
            return ActionResult(
                success=False, error=f"Element not found: {selector}", selector_found=False
            )
        try:
            await self._behavior.human_click(self._page, el)
            await self._behavior.between_actions_pause()
            return ActionResult(success=True)
        except Exception as exc:
            return ActionResult(success=False, error=str(exc)[:300])

    async def fill(self, selector: str, text: str) -> ActionResult:
        """Clear a field and type *text* with realistic cadence."""
        el = await self._resolve(selector)
        if el is None:
            return ActionResult(
                success=False, error=f"Element not found: {selector}", selector_found=False
            )
        try:
            await self._behavior.human_click(self._page, el)
            await el.evaluate("el => el.value = ''")
            await self._behavior.type_with_cadence(el, text)
            await self._behavior.between_actions_pause()
            return ActionResult(success=True)
        except Exception as exc:
            return ActionResult(success=False, error=str(exc)[:300])

    async def type_text(self, selector: str, text: str) -> ActionResult:
        """Append *text* to a field without clearing (trigger typing handlers)."""
        el = await self._resolve(selector)
        if el is None:
            return ActionResult(
                success=False, error=f"Element not found: {selector}", selector_found=False
            )
        try:
            await self._behavior.human_click(self._page, el)
            await self._behavior.type_with_cadence(el, text)
            await self._behavior.between_actions_pause()
            return ActionResult(success=True)
        except Exception as exc:
            return ActionResult(success=False, error=str(exc)[:300])

    async def select_option(self, selector: str, value: str) -> ActionResult:
        """Select a ``<select>`` option by value or visible text."""
        el = await self._resolve(selector)
        if el is None:
            return ActionResult(
                success=False, error=f"Element not found: {selector}", selector_found=False
            )
        try:
            await self._behavior.human_click(self._page, el)
            await self._page.select_option(selector, value=value, timeout=3_000)
            await self._behavior.between_actions_pause()
            return ActionResult(success=True)
        except Exception:
            try:
                await self._page.select_option(selector, label=value, timeout=3_000)
                await self._behavior.between_actions_pause()
                return ActionResult(success=True)
            except Exception as exc:
                return ActionResult(success=False, error=str(exc)[:300])

    async def check(self, selector: str) -> ActionResult:
        """Check a checkbox or radio button."""
        el = await self._resolve(selector)
        if el is None:
            return ActionResult(
                success=False, error=f"Element not found: {selector}", selector_found=False
            )
        try:
            is_checked: bool = await el.is_checked()
            if not is_checked:
                await self._behavior.human_click(self._page, el)
            await self._behavior.between_actions_pause()
            return ActionResult(success=True)
        except Exception as exc:
            return ActionResult(success=False, error=str(exc)[:300])

    async def upload_file(self, selector: str, path: str) -> ActionResult:
        """Upload a file via a file-input element."""
        el = await self._resolve(selector)
        if el is None:
            return ActionResult(
                success=False, error=f"Element not found: {selector}", selector_found=False
            )
        try:
            await el.set_input_files(path)
            await self._behavior.between_actions_pause()
            return ActionResult(success=True)
        except Exception as exc:
            return ActionResult(success=False, error=str(exc)[:300])

    async def scroll(self, direction: str = "down", amount: int = 500) -> ActionResult:
        """Scroll the page with human-like variance."""
        try:
            jitter = random.randint(-120, 120)
            pixels = max(120, amount + jitter)
            sign = -1 if direction == "up" else 1
            remaining = pixels
            while remaining > 0:
                step = min(remaining, random.randint(140, 420))
                await self._page.evaluate("([delta]) => window.scrollBy(0, delta)", [sign * step])
                remaining -= step
                await self._behavior.between_actions_pause(min_s=0.08, max_s=0.25)
            await self._behavior.between_actions_pause(min_s=0.2, max_s=0.7)
            return ActionResult(success=True)
        except Exception as exc:
            return ActionResult(success=False, error=str(exc)[:300])

    async def press_key(self, key: str) -> ActionResult:
        """Press a keyboard key (e.g. ``Enter``, ``Tab``, ``Escape``)."""
        try:
            await self._page.keyboard.press(key)
            await self._behavior.between_actions_pause(min_s=0.15, max_s=0.5)
            return ActionResult(success=True)
        except Exception as exc:
            return ActionResult(success=False, error=str(exc)[:300])

    # ------------------------------------------------------------------
    # Compound helpers
    # ------------------------------------------------------------------

    async def fill_and_tab(self, selector: str, text: str) -> ActionResult:
        """Fill a field then press Tab to trigger blur/validation events."""
        result = await self.fill(selector, text)
        if result.success:
            await self.press_key("Tab")
        return result

    async def click_and_wait(
        self, selector: str, wait_selector: str, *, timeout_ms: int = 5_000
    ) -> ActionResult:
        """Click an element and wait for another to appear."""
        result = await self.click(selector)
        if result.success:
            found = await self.wait_for(wait_selector, timeout_ms=timeout_ms)
            if not found:
                return ActionResult(
                    success=False,
                    error=f"Clicked {selector} but {wait_selector} did not appear",
                )
        return result
