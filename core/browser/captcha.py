"""CAPTCHA detection and response handling.

Primary strategy is AVOIDANCE: persistent sessions + realistic behavior means
CAPTCHAs rarely appear for established accounts. When they do appear:

Tier 1 (always): Wait 5-10s and re-detect. Cloudflare JS challenges often
resolve on their own after the browser passes the JS check.

Tier 2 (captcha_strategy='pause_notify'): Log warning, skip this provider for
the current run, send notification email if notifications are configured.

Tier 3 (captcha_strategy='solve'): Submit to 2captcha/anticaptcha API. Only
for reCAPTCHA v2 (visual checkbox type). Do not attempt to solve reCAPTCHA v3
or Cloudflare challenges via solver — they require page interaction that solvers
cannot fully automate.

The most important behavior: NEVER get stuck in a retry loop that keeps hitting
a CAPTCHA. Detect once, handle, move on.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from collections.abc import Callable

    from playwright.async_api import Page

logger = structlog.get_logger(__name__)


class CaptchaType(StrEnum):
    """Known CAPTCHA / challenge types."""

    RECAPTCHA_V2 = "recaptcha_v2"
    RECAPTCHA_V3 = "recaptcha_v3"
    HCAPTCHA = "hcaptcha"
    CLOUDFLARE_JS = "cloudflare_js"
    CLOUDFLARE_TURNSTILE = "cloudflare_turnstile"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CaptchaDetection:
    """Result of scanning a page for CAPTCHAs."""

    detected: bool
    captcha_type: CaptchaType | None = None
    page_url: str = ""


@dataclass(frozen=True)
class CaptchaOutcome:
    """Result of attempting to handle a detected CAPTCHA."""

    resolved: bool
    strategy_used: str
    error: str = ""


class CaptchaHandler:
    """Detect and respond to CAPTCHAs on provider pages."""

    async def detect(self, page: Page) -> CaptchaDetection:
        """Scan the current page for known CAPTCHA / challenge patterns."""
        url = page.url

        try:
            title = (await page.title()).lower()
            if "just a moment" in title or "attention required" in title:
                return CaptchaDetection(
                    detected=True,
                    captcha_type=CaptchaType.CLOUDFLARE_JS,
                    page_url=url,
                )
        except Exception:
            pass

        selector_checks: list[tuple[str, CaptchaType]] = [
            ("#challenge-form, .cf-challenge-running", CaptchaType.CLOUDFLARE_JS),
            (
                "iframe[src*='challenges.cloudflare.com']",
                CaptchaType.CLOUDFLARE_TURNSTILE,
            ),
            (
                "iframe[src*='google.com/recaptcha'][src*='anchor']",
                CaptchaType.RECAPTCHA_V2,
            ),
            (
                "iframe[src*='google.com/recaptcha'][src*='bframe']",
                CaptchaType.RECAPTCHA_V3,
            ),
            (".g-recaptcha", CaptchaType.RECAPTCHA_V2),
            ("iframe[src*='hcaptcha.com']", CaptchaType.HCAPTCHA),
            ("#captcha, .captcha-container", CaptchaType.UNKNOWN),
        ]

        for selector, captcha_type in selector_checks:
            try:
                el = await page.query_selector(selector)
                if el is not None:
                    return CaptchaDetection(detected=True, captcha_type=captcha_type, page_url=url)
            except Exception:
                continue

        return CaptchaDetection(detected=False)

    async def handle(
        self,
        page: Page,
        detection: CaptchaDetection,
        *,
        strategy: str,
        api_key: str = "",
        notification_fn: Callable[..., object] | None = None,
    ) -> CaptchaOutcome:
        """Attempt to resolve a detected CAPTCHA using the configured strategy."""
        if not detection.detected:
            return CaptchaOutcome(resolved=True, strategy_used="none")

        logger.warning(
            "captcha_detected",
            captcha_type=detection.captcha_type.value if detection.captcha_type else "unknown",
            page_url=detection.page_url,
            strategy=strategy,
        )

        if detection.captcha_type in (
            CaptchaType.CLOUDFLARE_JS,
            CaptchaType.CLOUDFLARE_TURNSTILE,
        ):
            await asyncio.sleep(8)
            recheck = await self.detect(page)
            if not recheck.detected:
                logger.info("captcha_resolved", strategy_used="wait_resolved")
                return CaptchaOutcome(resolved=True, strategy_used="wait_resolved")

        if strategy == "avoid":
            return CaptchaOutcome(resolved=False, strategy_used="skipped")

        if strategy == "pause_notify":
            if notification_fn is not None:
                try:
                    notification_fn(
                        f"CAPTCHA detected ({detection.captcha_type}) at {detection.page_url}"
                    )
                except Exception:
                    logger.warning("captcha_notification_failed", exc_info=True)
            return CaptchaOutcome(resolved=False, strategy_used="skipped")

        if strategy == "solve":
            if detection.captcha_type == CaptchaType.RECAPTCHA_V2 and api_key:
                return await self._solve_recaptcha_v2(page, detection, api_key=api_key)
            return CaptchaOutcome(
                resolved=False,
                strategy_used="unsolvable",
                error=f"Cannot solve {detection.captcha_type}",
            )

        return CaptchaOutcome(resolved=False, strategy_used="skipped")

    async def _solve_recaptcha_v2(
        self,
        page: Page,
        detection: CaptchaDetection,
        *,
        api_key: str,
    ) -> CaptchaOutcome:
        """Submit reCAPTCHA v2 to 2captcha API and inject the response token."""
        try:
            import httpx
        except ImportError:
            return CaptchaOutcome(
                resolved=False,
                strategy_used="unsolvable",
                error="httpx not installed",
            )

        try:
            sitekey: str = await page.evaluate(
                "() => document.querySelector('.g-recaptcha')?.getAttribute('data-sitekey') || ''"
            )
            if not sitekey:
                return CaptchaOutcome(
                    resolved=False,
                    strategy_used="unsolvable",
                    error="No data-sitekey found",
                )

            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "http://2captcha.com/in.php",
                    data={
                        "key": api_key,
                        "method": "userrecaptcha",
                        "googlekey": sitekey,
                        "pageurl": detection.page_url,
                        "json": "1",
                    },
                )
                result = resp.json()
                if result.get("status") != 1:
                    return CaptchaOutcome(
                        resolved=False,
                        strategy_used="unsolvable",
                        error=f"2captcha submit error: {result}",
                    )

                captcha_id = result["request"]

                for _ in range(24):
                    await asyncio.sleep(5)
                    poll = await client.get(
                        "http://2captcha.com/res.php",
                        params={
                            "key": api_key,
                            "action": "get",
                            "id": captcha_id,
                            "json": "1",
                        },
                    )
                    poll_result = poll.json()
                    if poll_result.get("status") == 1:
                        token = poll_result["request"]
                        await page.evaluate(
                            "t => { document.getElementById('g-recaptcha-response')"
                            ".innerHTML = t; }",
                            token,
                        )
                        await page.evaluate(
                            "document.getElementById('recaptcha-form') "
                            "&& document.getElementById('recaptcha-form').submit()"
                        )
                        logger.info("captcha_solved", strategy_used="solved")
                        return CaptchaOutcome(resolved=True, strategy_used="solved")
                    if poll_result.get("request") != "CAPCHA_NOT_READY":
                        return CaptchaOutcome(
                            resolved=False,
                            strategy_used="unsolvable",
                            error=f"2captcha poll error: {poll_result}",
                        )

                return CaptchaOutcome(
                    resolved=False,
                    strategy_used="unsolvable",
                    error="2captcha timeout",
                )
        except Exception as exc:
            return CaptchaOutcome(resolved=False, strategy_used="unsolvable", error=str(exc))
