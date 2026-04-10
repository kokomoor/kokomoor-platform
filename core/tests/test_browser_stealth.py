"""Tests for page-level anti-detection script invariants."""

from __future__ import annotations

from core.browser.stealth import ANTI_DETECTION_SCRIPT


class TestAntiDetectionScript:
    def test_hides_webdriver_as_undefined(self) -> None:
        assert "navigator, 'webdriver'" in ANTI_DETECTION_SCRIPT
        assert "get: () => undefined" in ANTI_DETECTION_SCRIPT

    def test_spoofs_plugins(self) -> None:
        assert "navigator, 'plugins'" in ANTI_DETECTION_SCRIPT
        assert "Chrome PDF Plugin" in ANTI_DETECTION_SCRIPT

    def test_spoofs_webgl1_and_webgl2(self) -> None:
        assert "WebGLRenderingContext.prototype.getParameter" in ANTI_DETECTION_SCRIPT
        assert "WebGL2RenderingContext.prototype.getParameter" in ANTI_DETECTION_SCRIPT
        assert "param === 37445" in ANTI_DETECTION_SCRIPT
        assert "param === 37446" in ANTI_DETECTION_SCRIPT

    def test_canvas_noise_is_per_call_not_fixed_offset(self) -> None:
        assert "channelNoise" in ANTI_DETECTION_SCRIPT
        assert "crypto.getRandomValues" in ANTI_DETECTION_SCRIPT

    def test_spoofs_chrome_runtime_surface(self) -> None:
        assert "window.chrome" in ANTI_DETECTION_SCRIPT
        assert "chrome.runtime" in ANTI_DETECTION_SCRIPT
