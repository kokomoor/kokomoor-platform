"""Browser-based ``ContentFetcher`` using ``BrowserManager`` (Playwright)."""

from __future__ import annotations

import structlog

from core.browser import BrowserManager
from core.config import get_settings
from core.fetch.types import FetchMethod, FetchResult

logger = structlog.get_logger(__name__)


class BrowserFetcher:
    """Render the page in a real browser and return ``document.documentElement`` HTML."""

    def __init__(
        self,
        *,
        post_wait_ms: int | None = None,
    ) -> None:
        settings = get_settings()
        self._post_wait_ms = (
            post_wait_ms if post_wait_ms is not None else settings.fetch_browser_post_wait_ms
        )

    async def fetch(self, url: str) -> FetchResult:
        """Navigate to *url*, wait briefly for JS, return ``page.content()``."""
        async with BrowserManager() as browser:
            page = await browser.new_page()
            await browser.rate_limited_goto(page, url)
            if self._post_wait_ms > 0:
                await page.wait_for_timeout(self._post_wait_ms)
            html = await page.content()
            final_url = page.url
            logger.info(
                "fetch_browser_complete",
                url=final_url,
                method=FetchMethod.BROWSER.value,
            )
            return FetchResult(
                html=html,
                final_url=final_url,
                status_code=200,
                method=FetchMethod.BROWSER,
            )
