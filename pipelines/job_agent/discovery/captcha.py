"""Compatibility shim — re-exports from core.browser.captcha.

All new code should import directly from ``core.browser.captcha``.
"""

from core.browser.captcha import (
    CaptchaDetection,
    CaptchaHandler,
    CaptchaOutcome,
    CaptchaType,
)

__all__ = ["CaptchaDetection", "CaptchaHandler", "CaptchaOutcome", "CaptchaType"]
