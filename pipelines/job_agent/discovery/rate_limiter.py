"""Per-domain rate limiting for provider scrapers.

Provider-specific timing profiles live here. The generic ``RateLimiter``
class has moved to ``core.browser.rate_limiter``.

``DomainRateLimiter`` is a thin wrapper that maps ``JobSource`` to a
``RateLimitProfile`` and delegates to the core class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.browser.rate_limiter import RateLimiter, RateLimitProfile

if TYPE_CHECKING:
    from pipelines.job_agent.models import JobSource

DomainRateLimit = RateLimitProfile

PROVIDER_LIMITS: dict[str, RateLimitProfile] = {
    "linkedin": RateLimitProfile(
        min_delay_s=10.0,
        max_delay_s=25.0,
        pages_before_long_pause=5,
        long_pause_min_s=45.0,
        long_pause_max_s=120.0,
    ),
    "indeed": RateLimitProfile(
        min_delay_s=5.0,
        max_delay_s=14.0,
        pages_before_long_pause=8,
        long_pause_min_s=30.0,
        long_pause_max_s=75.0,
    ),
    "builtin": RateLimitProfile(
        min_delay_s=3.0,
        max_delay_s=8.0,
        pages_before_long_pause=15,
        long_pause_min_s=20.0,
        long_pause_max_s=50.0,
    ),
    "wellfound": RateLimitProfile(
        min_delay_s=4.0,
        max_delay_s=10.0,
        pages_before_long_pause=10,
        long_pause_min_s=25.0,
        long_pause_max_s=60.0,
    ),
    "workday": RateLimitProfile(
        min_delay_s=3.0,
        max_delay_s=9.0,
        pages_before_long_pause=12,
        long_pause_min_s=25.0,
        long_pause_max_s=60.0,
    ),
    "greenhouse": RateLimitProfile(
        min_delay_s=0.5,
        max_delay_s=2.0,
        pages_before_long_pause=50,
    ),
    "lever": RateLimitProfile(
        min_delay_s=0.5,
        max_delay_s=2.0,
        pages_before_long_pause=50,
    ),
}

_DEFAULT_PROFILE = RateLimitProfile(min_delay_s=4.0, max_delay_s=10.0)


class DomainRateLimiter(RateLimiter):
    """Rate limiter that resolves timing profile from ``JobSource``."""

    def __init__(self, source: JobSource) -> None:
        source_key = source.value if hasattr(source, "value") else str(source)
        profile = PROVIDER_LIMITS.get(source_key, _DEFAULT_PROFILE)
        super().__init__(source=source_key, profile=profile)
