"""Per-domain rate limiting for provider scrapers.

Token bucket with provider-specific delays and periodic long pauses.
Separate from the global BrowserManager rate limit (which is a floor, not a ceiling).
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass

import structlog

from pipelines.job_agent.models import JobSource

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class DomainRateLimit:
    """Rate-limit profile for a single provider domain."""

    min_delay_s: float
    max_delay_s: float
    pages_before_long_pause: int = 8
    long_pause_min_s: float = 35.0
    long_pause_max_s: float = 90.0


PROVIDER_LIMITS: dict[JobSource, DomainRateLimit] = {
    JobSource.LINKEDIN: DomainRateLimit(
        min_delay_s=10.0,
        max_delay_s=25.0,
        pages_before_long_pause=5,
        long_pause_min_s=45.0,
        long_pause_max_s=120.0,
    ),
    JobSource.INDEED: DomainRateLimit(
        min_delay_s=5.0,
        max_delay_s=14.0,
        pages_before_long_pause=8,
        long_pause_min_s=30.0,
        long_pause_max_s=75.0,
    ),
    JobSource.BUILTIN: DomainRateLimit(
        min_delay_s=3.0,
        max_delay_s=8.0,
        pages_before_long_pause=15,
        long_pause_min_s=20.0,
        long_pause_max_s=50.0,
    ),
    JobSource.WELLFOUND: DomainRateLimit(
        min_delay_s=4.0,
        max_delay_s=10.0,
        pages_before_long_pause=10,
        long_pause_min_s=25.0,
        long_pause_max_s=60.0,
    ),
    JobSource.WORKDAY: DomainRateLimit(
        min_delay_s=3.0,
        max_delay_s=9.0,
        pages_before_long_pause=12,
        long_pause_min_s=25.0,
        long_pause_max_s=60.0,
    ),
    JobSource.GREENHOUSE: DomainRateLimit(
        min_delay_s=0.5,
        max_delay_s=2.0,
        pages_before_long_pause=50,
    ),
    JobSource.LEVER: DomainRateLimit(
        min_delay_s=0.5,
        max_delay_s=2.0,
        pages_before_long_pause=50,
    ),
}

_DEFAULT_LIMIT = DomainRateLimit(min_delay_s=4.0, max_delay_s=10.0)


class DomainRateLimiter:
    """Async rate limiter scoped to a single provider."""

    def __init__(self, source: JobSource) -> None:
        self._source = source
        self._limit = PROVIDER_LIMITS.get(source, _DEFAULT_LIMIT)
        self._page_count = 0

    @property
    def page_count(self) -> int:
        return self._page_count

    async def wait(self) -> None:
        """Sleep for an appropriate delay, with periodic long pauses."""
        self._page_count += 1

        if self._page_count % self._limit.pages_before_long_pause == 0:
            delay = random.uniform(self._limit.long_pause_min_s, self._limit.long_pause_max_s)
            logger.info(
                "rate_limit_long_pause",
                source=self._source.value,
                delay_s=round(delay, 1),
                page_count=self._page_count,
            )
        else:
            delay = random.uniform(self._limit.min_delay_s, self._limit.max_delay_s)
            logger.debug(
                "rate_limit_wait",
                source=self._source.value,
                delay_s=round(delay, 1),
                page_count=self._page_count,
            )

        await asyncio.sleep(delay)
