"""Realistic human interaction simulation for Playwright pages.

Every interactive action taken by a browser automation MUST go through this
class. The goal is to produce browser behavior patterns that are
statistically indistinguishable from a human using Chrome on a laptop.

Key detection vectors this addresses:
- Mechanical timing (zero variance between actions) -> randomized delays with
  realistic distributions (Gaussian near human means, not uniform).
- Instantaneous mouse teleportation -> Bezier-curve mouse paths with velocity
  that accelerates mid-path and decelerates near target.
- Robotic scroll velocity -> variable-speed scroll with random micro-pauses.
- Zero reading time -> content-length-proportional pauses after page load.
- Perfectly consistent typing speed -> per-character timing with realistic
  cadence variance and a small typo-and-correct rate.
"""

from __future__ import annotations

import asyncio
import math
import random
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from typing import Any

    from playwright.async_api import Page

logger = structlog.get_logger(__name__)

_ADJACENT_KEYS: dict[str, str] = {
    "q": "w", "w": "e", "e": "r", "r": "t", "t": "y",
    "y": "u", "u": "i", "i": "o", "o": "p", "p": "o",
    "a": "s", "s": "d", "d": "f", "f": "g", "g": "h",
    "h": "j", "j": "k", "k": "l", "l": "k",
    "z": "x", "x": "c", "c": "v", "v": "b", "b": "n",
    "n": "m", "m": "n",
}  # fmt: skip


def _adjacent_key(char: str) -> str:
    """Return a plausible typo character for the given key."""
    return _ADJACENT_KEYS.get(char.lower(), char)


class HumanBehavior:
    """Simulate realistic human browser interactions."""

    async def reading_pause(self, content_length_chars: int) -> None:
        """Pause proportionally to content length, simulating reading."""
        words = content_length_chars / 5
        wpm = max(200.0, min(280.0, random.gauss(240, 30)))
        base_time = (words / wpm) * 60
        overhead = random.uniform(0.5, 2.0)
        total = max(1.5, min(10.0, base_time + overhead))
        logger.debug("reading_pause", estimated_words=int(words), sleep_s=round(total, 2))
        await asyncio.sleep(total)

    async def scroll_down_naturally(self, page: Page) -> None:
        """Scroll from top to bottom with human-like velocity variance."""
        total_height: int = await page.evaluate("document.body.scrollHeight")
        current = 0
        steps = 0

        while current < total_height:
            step = random.randint(180, 550)
            current = min(current + step, total_height)
            await page.evaluate(f"window.scrollTo(0, {current})")
            steps += 1
            await asyncio.sleep(random.uniform(0.08, 0.35))

            if random.random() < 0.12:
                await asyncio.sleep(random.uniform(0.8, 2.5))

            if random.random() < 0.05:
                back = random.randint(100, 300)
                current = max(0, current - back)
                await page.evaluate(f"window.scrollTo(0, {current})")
                await asyncio.sleep(random.uniform(0.1, 0.3))

        logger.debug("scroll_down", total_height=total_height, steps=steps)

    async def move_mouse_naturally(self, page: Page, target_x: float, target_y: float) -> None:
        """Move mouse along a quadratic Bezier curve with deceleration."""
        sx, sy = self._last_x, self._last_y

        mx, my = (sx + target_x) / 2, (sy + target_y) / 2
        cx = mx + random.uniform(-60, 60)
        cy = my + random.uniform(-60, 60)

        num_steps = random.randint(25, 45)
        for i in range(num_steps + 1):
            t = i / num_steps
            x = (1 - t) ** 2 * sx + 2 * (1 - t) * t * cx + t**2 * target_x
            y = (1 - t) ** 2 * sy + 2 * (1 - t) * t * cy + t**2 * target_y
            await page.mouse.move(x, y)

            if i >= num_steps - 5:
                await asyncio.sleep(random.uniform(0.012, 0.025))
            else:
                await asyncio.sleep(random.uniform(0.006, 0.018))

        self._last_x, self._last_y = target_x, target_y

    async def human_click(self, page: Page, element: Any) -> None:
        """Click an element with natural mouse movement and slight offset."""
        box = await element.bounding_box()
        if box is None:
            await element.click()
            return

        margin_x = box["width"] * 0.2
        margin_y = box["height"] * 0.2
        click_x = box["x"] + margin_x + random.uniform(0, box["width"] * 0.6)
        click_y = box["y"] + margin_y + random.uniform(0, box["height"] * 0.6)

        await self.move_mouse_naturally(page, click_x, click_y)
        await asyncio.sleep(random.uniform(0.04, 0.2))

        if random.random() < 0.03:
            await page.mouse.dblclick(click_x, click_y)
        else:
            await page.mouse.click(click_x, click_y)

    async def type_with_cadence(self, element: Any, text: str) -> None:
        """Type text character-by-character with realistic cadence and typos."""
        chars_since_pause = 0
        next_pause_at = random.randint(4, 8)

        for char in text:
            if random.random() < 0.025:
                await element.press(_adjacent_key(char))
                await asyncio.sleep(random.uniform(0.15, 0.4))
                await element.press("Backspace")
                await asyncio.sleep(random.uniform(0.08, 0.2))

            base_delay = random.lognormvariate(math.log(90), 0.4) / 1000.0
            await element.press(char)
            await asyncio.sleep(base_delay)

            chars_since_pause += 1
            if chars_since_pause >= next_pause_at:
                await asyncio.sleep(random.uniform(0, 0.15))
                chars_since_pause = 0
                next_pause_at = random.randint(4, 8)

            if char == " " and random.random() < 0.2:
                await asyncio.sleep(random.uniform(0, 0.25))

    async def between_actions_pause(self, *, min_s: float = 0.3, max_s: float = 1.5) -> None:
        """Random pause between discrete user actions."""
        await asyncio.sleep(random.uniform(min_s, max_s))

    async def between_navigations_pause(self, *, min_s: float = 0.5, max_s: float = 2.5) -> None:
        """Extra jitter after rate-limiter wait to break timing patterns."""
        jitter = random.uniform(min_s, max_s)
        logger.debug("between_navigations_jitter", jitter_s=round(jitter, 2))
        await asyncio.sleep(jitter)

    async def simulate_interest_in_page(self, page: Page) -> None:
        """Compose scroll + pause to look like a human browsing results."""
        await self.scroll_down_naturally(page)
        if random.random() < 0.03:
            height: int = await page.evaluate("document.body.scrollHeight")
            scroll_to = random.randint(0, max(0, int(height * 0.7)))
            await page.evaluate(f"window.scrollTo(0, {scroll_to})")
            await asyncio.sleep(random.uniform(0.5, 1.5))
        await self.between_actions_pause()

    async def hover_before_click(self, page: Page, element: Any) -> None:
        """Move mouse to element and pause (read label) without clicking."""
        box = await element.bounding_box()
        if box is None:
            return
        hover_x = box["x"] + box["width"] / 2 + random.uniform(-5, 5)
        hover_y = box["y"] + box["height"] / 2 + random.uniform(-5, 5)
        await self.move_mouse_naturally(page, hover_x, hover_y)
        await asyncio.sleep(random.uniform(0.2, 0.6))
