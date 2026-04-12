"""Process-wide throttling for Anthropic API calls.

Two layered primitives that every ``AnthropicClient`` instance shares so
multiple nodes in the same pipeline cannot accidentally burst past the
org's rate limits:

1. ``global_semaphore`` — a plain asyncio semaphore that caps the number
   of in-flight ``messages.create`` calls across the whole process. The
   per-engine ``llm_max_concurrency`` knob only caps a single node's fan
   out, so if cover-letter and resume tailoring ever ran in parallel
   they could double-up. A process-wide cap closes that loophole.

2. ``TokenBucket`` — a rolling-window limiter that approximates the
   Anthropic per-minute *output token* ceiling. Each call reserves its
   ``max_tokens`` budget up front; when the 60 second window is full
   the call awaits until the oldest reservation expires. This keeps us
   from firing a burst that will deterministically 429 even though no
   single request is too large.

Both primitives are created lazily inside ``get_throttle()`` so test
environments that never touch the LLM don't pay a cost for them.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class _Reservation:
    expires_at: float
    tokens: int


class TokenBucket:
    """Rolling 60 second output-token budget shared across tasks.

    Each call reserves ``tokens`` budget for 60 seconds. If granting a
    new reservation would push the in-window total over ``limit``, the
    caller awaits until the oldest reservation expires. The bucket is
    coroutine-safe via an internal ``asyncio.Lock``.
    """

    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        self._limit = max(1, int(limit))
        self._window = window_seconds
        self._lock = asyncio.Lock()
        self._reservations: deque[_Reservation] = deque()

    def _purge(self, now: float) -> None:
        while self._reservations and self._reservations[0].expires_at <= now:
            self._reservations.popleft()

    def _in_window_total(self) -> int:
        return sum(r.tokens for r in self._reservations)

    async def acquire(self, tokens: int) -> None:
        """Block until ``tokens`` budget is available in the window."""
        tokens = max(1, int(tokens))
        while True:
            async with self._lock:
                now = time.monotonic()
                self._purge(now)
                current = self._in_window_total()
                if current + tokens <= self._limit:
                    self._reservations.append(
                        _Reservation(expires_at=now + self._window, tokens=tokens)
                    )
                    return
                # Not enough room; sleep until the oldest reservation
                # expires, then try again.
                oldest = self._reservations[0]
                wait_s = max(0.01, oldest.expires_at - now)
            logger.info(
                "llm_token_bucket_wait",
                wait_s=round(wait_s, 2),
                requested=tokens,
                limit=self._limit,
            )
            await asyncio.sleep(wait_s)


@dataclass
class Throttle:
    """Bundle of the process-wide LLM rate-limit primitives."""

    global_semaphore: asyncio.Semaphore
    output_token_bucket: TokenBucket


_throttle: Throttle | None = None


def get_throttle(
    *,
    max_concurrent_requests: int,
    output_tokens_per_minute: int,
) -> Throttle:
    """Return the process-wide throttle, initialising it on first use.

    The first caller's parameters win; subsequent calls return the
    already-initialised singleton so the whole process shares one
    semaphore and one token bucket. Tests that need a fresh instance
    should call :func:`reset_throttle`.
    """
    global _throttle
    if _throttle is None:
        _throttle = Throttle(
            global_semaphore=asyncio.Semaphore(max(1, max_concurrent_requests)),
            output_token_bucket=TokenBucket(limit=output_tokens_per_minute),
        )
    return _throttle


def reset_throttle() -> None:
    """Drop the throttle singleton (tests only)."""
    global _throttle
    _throttle = None
