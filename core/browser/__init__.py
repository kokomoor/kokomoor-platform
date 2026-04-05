"""Playwright browser lifecycle management.

Provides a managed async browser context with anti-detection defaults,
rate limiting, and proper resource cleanup. All pipelines that need
web interaction share this abstraction.

Usage:
    from core.browser import BrowserManager

    async with BrowserManager() as browser:
        page = await browser.new_page()
        await page.goto("https://example.com")
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog
from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from core.browser.stealth import apply_stealth_defaults
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

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._last_navigation: float = 0.0
        self._settings = get_settings()

    async def __aenter__(self) -> BrowserManager:
        """Launch the browser and create a context."""
        pw = await async_playwright().start()
        self._playwright = pw
        self._browser = await pw.chromium.launch(
            headless=self._settings.browser_headless,
        )
        context_options = apply_stealth_defaults() if self._settings.enable_browser_stealth else {}
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
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("browser_stopped")

    async def new_page(self) -> Page:
        """Create a new page in the managed browser context."""
        if self._context is None:
            msg = "BrowserManager must be used as an async context manager."
            raise RuntimeError(msg)
        return await self._context.new_page()

    async def rate_limited_goto(self, page: Page, url: str) -> None:
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
        await page.goto(url, wait_until="domcontentloaded")
        self._last_navigation = asyncio.get_event_loop().time()
