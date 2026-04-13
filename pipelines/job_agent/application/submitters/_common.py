"""Common utilities for API-based application submitters."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    import httpx

logger = structlog.get_logger(__name__)

_MAX_429_RETRIES = 3
_DEFAULT_429_BACKOFF_SECONDS = 5.0


async def post_with_backoff(
    client: httpx.AsyncClient,
    url: str,
    *,
    data: dict[str, str],
    files: dict[str, tuple[str, bytes, str]],
    source: str = "api",
) -> httpx.Response:
    """POST to an endpoint with adaptive backoff on 429 Rate Limit responses."""
    resp = await client.post(url, data=data, files=files)
    for attempt in range(_MAX_429_RETRIES):
        if resp.status_code != 429:
            return resp
        wait_raw = resp.headers.get("Retry-After", "")
        wait = float(wait_raw) if wait_raw.isdigit() else _DEFAULT_429_BACKOFF_SECONDS
        logger.warning(
            f"{source}_429_retry",
            attempt=attempt + 1,
            retry_after=wait,
            url=url,
        )
        await asyncio.sleep(wait)
        resp = await client.post(url, data=data, files=files)
    return resp
