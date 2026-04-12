"""Generic rate limiting for browser automation.

Provides configurable delays with periodic long pauses to simulate
realistic browsing patterns. Domain-specific profiles (e.g. per source)
are provided by the caller, keeping this module domain-agnostic.

Includes adaptive rate limiting that responds to 429s, Retry-After headers,
and per-route budgets.
"""

from __future__ import annotations

import asyncio
import random
import time
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

_BACKOFF_MULTIPLIER = 1.5
_MAX_ADAPTIVE_DELAY = 120.0
_COOLDOWN_WINDOW_S = 600.0


class RateLimiter:
    """Async rate limiter with adaptive feedback.

    Each call to ``wait()`` increments a page counter and sleeps for
    a random delay. Every ``pages_before_long_pause`` pages, a longer
    pause is inserted to break predictable timing patterns.

    Adaptive features:
    - ``report_429()`` — backs off exponentially when the server returns 429.
    - ``report_retry_after(seconds)`` — honors Retry-After headers.
    - Per-route budgets (optional) limit requests to specific URL paths.
    """

    def __init__(self, source: str, profile: RateLimitProfile | None = None) -> None:
        self._source = source
        self._profile = profile or DEFAULT_PROFILE
        self._page_count = 0
        self._adaptive_multiplier: float = 1.0
        self._retry_after_until: float = 0.0
        self._consecutive_429s: int = 0
        self._route_budgets: dict[str, _RouteBudget] = {}

    @property
    def page_count(self) -> int:
        return self._page_count

    @property
    def adaptive_multiplier(self) -> float:
        return self._adaptive_multiplier

    async def wait(self, *, route: str = "") -> None:
        """Sleep for an appropriate delay, with periodic long pauses."""
        self._page_count += 1

        now = time.monotonic()
        if self._retry_after_until > now:
            wait_for_retry = self._retry_after_until - now
            logger.info(
                "rate_limit_retry_after",
                source=self._source,
                wait_s=round(wait_for_retry, 1),
            )
            await asyncio.sleep(wait_for_retry)

        if route and route in self._route_budgets:
            budget = self._route_budgets[route]
            if budget.exhausted:
                logger.warning(
                    "rate_limit_route_exhausted",
                    source=self._source,
                    route=route,
                    budget=budget.max_requests,
                )
                cooldown = min(self._profile.max_delay_s * 2, _MAX_ADAPTIVE_DELAY)
                await asyncio.sleep(cooldown)
                return
            budget.count += 1

        if self._page_count % self._profile.pages_before_long_pause == 0:
            delay = random.uniform(self._profile.long_pause_min_s, self._profile.long_pause_max_s)
            logger.info(
                "rate_limit_long_pause",
                source=self._source,
                delay_s=round(delay, 1),
                page_count=self._page_count,
            )
        else:
            base_delay = random.uniform(self._profile.min_delay_s, self._profile.max_delay_s)
            delay = min(base_delay * self._adaptive_multiplier, _MAX_ADAPTIVE_DELAY)
            logger.debug(
                "rate_limit_wait",
                source=self._source,
                delay_s=round(delay, 1),
                page_count=self._page_count,
                multiplier=round(self._adaptive_multiplier, 2),
            )

        await asyncio.sleep(delay)

    def report_429(self) -> None:
        """Report a 429 response — back off exponentially."""
        self._consecutive_429s += 1
        self._adaptive_multiplier = min(
            self._adaptive_multiplier * _BACKOFF_MULTIPLIER,
            _MAX_ADAPTIVE_DELAY / max(self._profile.max_delay_s, 1),
        )
        logger.warning(
            "rate_limit_429_backoff",
            source=self._source,
            consecutive=self._consecutive_429s,
            new_multiplier=round(self._adaptive_multiplier, 2),
        )

    def report_retry_after(self, seconds: float) -> None:
        """Honor a Retry-After header."""
        self._retry_after_until = time.monotonic() + seconds
        logger.info(
            "rate_limit_retry_after_set",
            source=self._source,
            wait_s=round(seconds, 1),
        )

    def report_success(self) -> None:
        """Report a successful request — gradually reduce backoff."""
        if self._consecutive_429s > 0:
            self._consecutive_429s = max(0, self._consecutive_429s - 1)
        if self._adaptive_multiplier > 1.0:
            self._adaptive_multiplier = max(1.0, self._adaptive_multiplier / 1.1)

    def set_route_budget(self, route: str, max_requests: int) -> None:
        """Set a per-route request budget."""
        self._route_budgets[route] = _RouteBudget(max_requests=max_requests)

    def reset_route_budgets(self) -> None:
        """Reset all per-route budgets."""
        self._route_budgets.clear()


@dataclass
class _RouteBudget:
    """Per-route request counter."""

    max_requests: int
    count: int = 0

    @property
    def exhausted(self) -> bool:
        return self.count >= self.max_requests
