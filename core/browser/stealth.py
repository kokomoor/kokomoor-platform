"""Anti-detection defaults for Playwright browser contexts.

Generates randomised but realistic browser fingerprints to reduce the
likelihood of bot detection on job boards and other protected sites.
This is not about being adversarial — it's about not looking like a
headless Chrome instance running on a Ryzen server.
"""

from __future__ import annotations

import random
from typing import Any

# Realistic desktop user agents (rotate per session).
_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

# Common desktop viewport sizes.
_VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1680, "height": 1050},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1366, "height": 768},
]

# Timezones for US-based browsing.
_TIMEZONES = [
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
]

_LOCALES = ["en-US"]


def apply_stealth_defaults() -> dict[str, Any]:
    """Return randomised but realistic browser context options.

    These are passed directly to ``browser.new_context(**options)``.
    Each call produces a slightly different fingerprint to avoid
    pattern-based detection across sessions.
    """
    viewport = random.choice(_VIEWPORTS)

    return {
        "user_agent": random.choice(_USER_AGENTS),
        "viewport": viewport,
        "screen": viewport,  # Match screen to viewport.
        "timezone_id": random.choice(_TIMEZONES),
        "locale": random.choice(_LOCALES),
        "color_scheme": "light",
        "java_script_enabled": True,
        "has_touch": False,
        "is_mobile": False,
        "device_scale_factor": random.choice([1, 1.25, 1.5, 2]),
    }


async def human_delay(min_ms: int = 500, max_ms: int = 2000) -> None:
    """Sleep for a random human-realistic duration.

    Use between interactions (clicks, typing, scrolling) to avoid
    mechanical timing patterns.
    """
    import asyncio

    delay = random.randint(min_ms, max_ms) / 1000.0
    await asyncio.sleep(delay)
