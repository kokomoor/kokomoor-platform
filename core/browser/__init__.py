"""Playwright browser lifecycle management and shared browser infrastructure.

Provides a managed async browser context with anti-detection defaults,
rate limiting, and proper resource cleanup. All pipelines that need
web interaction share this abstraction.

Also re-exports shared browser automation utilities (human simulation,
CAPTCHA handling, session persistence, rate limiting, failure capture)
that were promoted from ``pipelines.job_agent.discovery`` to live here
as domain-agnostic core infrastructure.

Usage:
    from core.browser import BrowserManager
    from core.browser.human_behavior import HumanBehavior
    from core.browser.captcha import CaptchaHandler
    from core.browser.session import SessionStore
    from core.browser.rate_limiter import RateLimiter, RateLimitProfile
    from core.browser.debug_capture import FailureCapture

    async with BrowserManager() as browser:
        page = await browser.new_page()
        await page.goto("https://example.com")
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

import structlog
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Response,
    async_playwright,
)

from core.browser.stealth import apply_page_stealth, apply_stealth_defaults
from core.config import get_settings

if TYPE_CHECKING:
    from types import TracebackType

logger = structlog.get_logger(__name__)


class BrowserManager:
    """Managed Playwright browser with anti-detection and rate limiting.

    Use as an async context manager to ensure proper cleanup of browser
    resources. Rate limiting is enforced between page navigations to
    avoid triggering bot detection.
    """

    def __init__(self, *, storage_state: dict[str, Any] | None = None) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._last_navigation: float = 0.0
        self._settings = get_settings()
        self._storage_state = storage_state

    async def __aenter__(self) -> BrowserManager:
        """Launch the browser and create a context."""
        pw = await async_playwright().start()
        self._playwright = pw
        self._browser = await pw.chromium.launch(
            headless=self._settings.browser_headless,
        )
        context_options = apply_stealth_defaults() if self._settings.enable_browser_stealth else {}
        if self._storage_state is not None:
            context_options["storage_state"] = self._storage_state
        self._context = await self._browser.new_context(**context_options)

        logger.info(
            "browser_started",
            headless=self._settings.browser_headless,
            stealth=self._settings.enable_browser_stealth,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Close browser and playwright resources."""
        if self._context:
            try:
                await self._context.close()
            except Exception:
                logger.exception("browser_context_close_failed")
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                logger.exception("browser_close_failed")
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                logger.exception("playwright_stop_failed")
        logger.info("browser_stopped")

    async def new_page(self) -> Page:
        """Create a new page in the managed browser context."""
        if self._context is None:
            msg = "BrowserManager must be used as an async context manager."
            raise RuntimeError(msg)
        page = await self._context.new_page()
        if self._settings.enable_browser_stealth:
            await apply_page_stealth(page)
        return page

    async def dump_storage_state(self) -> dict[str, Any]:
        """Snapshot cookies, localStorage, and sessionStorage.

        Returns a dict suitable for passing back as ``storage_state``
        to a future ``BrowserManager`` to restore the session.
        """
        if self._context is None:
            msg = "BrowserManager must be used as an async context manager."
            raise RuntimeError(msg)
        return cast("dict[str, Any]", await self._context.storage_state())

    async def get_cookies(self, urls: list[str] | None = None) -> list[dict[str, Any]]:
        """Return cookies in the browser context, optionally filtered by URL list.

        Matches Playwright's ``BrowserContext.cookies(urls)`` signature.
        Returns an empty list when called outside a context manager.
        """
        if self._context is None:
            return []
        if urls:
            return cast("list[dict[str, Any]]", await self._context.cookies(urls))
        return cast("list[dict[str, Any]]", await self._context.cookies())

    async def rate_limited_goto(
        self,
        page: Page,
        url: str,
        *,
        timeout_ms: int | None = None,
    ) -> Response | None:
        """Navigate to a URL with rate limiting between requests.

        Enforces a minimum delay between navigations to avoid triggering
        bot detection on job boards and other protected sites.
        """
        now = asyncio.get_event_loop().time()
        elapsed = now - self._last_navigation
        min_delay = self._settings.browser_rate_limit_seconds

        if elapsed < min_delay:
            wait = min_delay - elapsed
            logger.debug("rate_limit_wait", wait_seconds=round(wait, 2))
            await asyncio.sleep(wait)

        logger.info("page_navigate", url=url)
        response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        self._last_navigation = asyncio.get_event_loop().time()
        return response
