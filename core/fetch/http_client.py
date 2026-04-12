"""HTTP-based ``ContentFetcher`` using httpx with retries and structured logging."""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from core.config import get_settings
from core.fetch.types import FetchMethod, FetchResult

logger = structlog.get_logger(__name__)

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


class HttpFetcher:
    """Fetch raw HTML or JSON over HTTPS with retries and timeouts."""

    def __init__(
        self,
        *,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
        default_headers: dict[str, str] | None = None,
    ) -> None:
        settings = get_settings()
        self._timeout = (
            timeout_seconds if timeout_seconds is not None else settings.fetch_http_timeout_seconds
        )
        self._max_retries = (
            max_retries if max_retries is not None else settings.fetch_http_max_retries
        )
        self._headers = default_headers if default_headers is not None else dict(_DEFAULT_HEADERS)

    async def fetch(self, url: str) -> FetchResult:
        """GET *url* and return HTML and final URL after redirects."""
        last_error = ""
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(self._timeout),
            headers=self._headers,
        ) as client:
            for attempt in range(self._max_retries + 1):
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    final_url = str(resp.url)
                    logger.info(
                        "fetch_http_complete",
                        url=final_url,
                        status=resp.status_code,
                        attempt=attempt + 1,
                    )
                    return FetchResult(
                        html=resp.text,
                        final_url=final_url,
                        status_code=resp.status_code,
                        method=FetchMethod.HTTP,
                    )
                except httpx.HTTPError as exc:
                    last_error = str(exc)
                    if attempt == self._max_retries:
                        break
                    logger.warning(
                        "fetch_http_retry",
                        url=url,
                        attempt=attempt + 1,
                        error=last_error[:160],
                    )

        msg = f"HTTP fetch failed after retries: {last_error}"
        raise ValueError(msg)

    async def fetch_json(self, url: str) -> Any:
        """GET *url*, parse as JSON, and return the decoded payload.

        Uses the same timeout, retry, and header configuration as ``fetch()``.
        Raises ``ValueError`` on network failure or non-2xx status after retries.
        """
        last_error = ""
        headers = dict(self._headers)
        headers["Accept"] = "application/json"

        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(self._timeout),
            headers=headers,
        ) as client:
            for attempt in range(self._max_retries + 1):
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    logger.info(
                        "fetch_json_complete",
                        url=str(resp.url),
                        status=resp.status_code,
                        attempt=attempt + 1,
                    )
                    return resp.json()
                except httpx.HTTPError as exc:
                    last_error = str(exc)
                    if attempt == self._max_retries:
                        break
                    logger.warning(
                        "fetch_json_retry",
                        url=url,
                        attempt=attempt + 1,
                        error=last_error[:160],
                    )

        msg = f"JSON fetch failed after retries: {last_error}"
        raise ValueError(msg)
