"""Compatibility shim — re-exports from core.browser.session.

All new code should import directly from ``core.browser.session``.
"""

from core.browser.session import SessionStore

__all__ = ["SessionStore"]
