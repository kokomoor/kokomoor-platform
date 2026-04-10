"""Compatibility shim — re-exports from core.browser.debug_capture.

All new code should import directly from ``core.browser.debug_capture``.
"""

from core.browser.debug_capture import FailureCapture

__all__ = ["FailureCapture"]
