"""Generic rate limiting for browser automation.

Provides configurable delays with periodic long pauses to simulate
realistic browsing patterns. Domain-specific profiles (e.g. per job board)
are provided by the caller, keeping this module domain-agnostic.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass

import structlog

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class RateLimitProfile:
    """Rate-limit profile for a single domain."""

    min_delay_s: float
    max_delay_s: float
    pages_before_long_pause: int = 8
    long_pause_min_s: float = 35.0
    long_pause_max_s: float = 90.0


DEFAULT_PROFILE = RateLimitProfile(min_delay_s=4.0, max_delay_s=10.0)


class RateLimiter:
    """Async rate limiter scoped to a single provider/domain.

    Each call to ``wait()`` increments a page counter and sleeps for
    a random delay. Every ``pages_before_long_pause`` pages, a longer
    pause is inserted to break predictable timing patterns.
    """

    def __init__(self, source: str, profile: RateLimitProfile | None = None) -> None:
        self._source = source
        self._profile = profile or DEFAULT_PROFILE
        self._page_count = 0

    @property
    def page_count(self) -> int:
        return self._page_count

    async def wait(self) -> None:
        """Sleep for an appropriate delay, with periodic long pauses."""
        self._page_count += 1

        if self._page_count % self._profile.pages_before_long_pause == 0:
            delay = random.uniform(self._profile.long_pause_min_s, self._profile.long_pause_max_s)
            logger.info(
                "rate_limit_long_pause",
                source=self._source,
                delay_s=round(delay, 1),
                page_count=self._page_count,
            )
        else:
            delay = random.uniform(self._profile.min_delay_s, self._profile.max_delay_s)
            logger.debug(
                "rate_limit_wait",
                source=self._source,
                delay_s=round(delay, 1),
                page_count=self._page_count,
            )

        await asyncio.sleep(delay)
