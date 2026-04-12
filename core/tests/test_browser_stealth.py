"""Tests for page-level anti-detection script invariants."""

from __future__ import annotations

from core.browser.stealth import ANTI_DETECTION_SCRIPT


class TestAntiDetectionScript:
    def test_hides_webdriver_as_false(self) -> None:
        assert "navigator, 'webdriver'" in ANTI_DETECTION_SCRIPT
        assert "get: () => false" in ANTI_DETECTION_SCRIPT

    def test_spoofs_plugins(self) -> None:
        assert "navigator, 'plugins'" in ANTI_DETECTION_SCRIPT
        assert "Chrome PDF Plugin" in ANTI_DETECTION_SCRIPT

    def test_spoofs_webgl1_and_webgl2(self) -> None:
        assert "WebGLRenderingContext.prototype.getParameter" in ANTI_DETECTION_SCRIPT
        assert "WebGL2RenderingContext.prototype.getParameter" in ANTI_DETECTION_SCRIPT
        assert "param === 37445" in ANTI_DETECTION_SCRIPT
        assert "param === 37446" in ANTI_DETECTION_SCRIPT

    def test_canvas_noise_is_deterministic_per_canvas(self) -> None:
        assert "canvasNoise = new WeakMap()" in ANTI_DETECTION_SCRIPT
        assert "canvasNoise.get(canvas)" in ANTI_DETECTION_SCRIPT
        assert "canvasNoise.set(canvas, noise)" in ANTI_DETECTION_SCRIPT

    def test_spoofs_chrome_runtime_surface(self) -> None:
        assert "window.chrome" in ANTI_DETECTION_SCRIPT
        assert "chrome.runtime" in ANTI_DETECTION_SCRIPT
        assert "connect:" in ANTI_DETECTION_SCRIPT
        assert "sendMessage:" in ANTI_DETECTION_SCRIPT
        assert "onConnect" in ANTI_DETECTION_SCRIPT
        assert "onMessage" in ANTI_DETECTION_SCRIPT
        assert "id: ''" in ANTI_DETECTION_SCRIPT

    def test_webgl_spoofing_is_platform_aware(self) -> None:
        assert "navigator.userAgent" in ANTI_DETECTION_SCRIPT
        assert "/Windows/" in ANTI_DETECTION_SCRIPT
        assert "/Linux/" in ANTI_DETECTION_SCRIPT
        assert "Mesa" in ANTI_DETECTION_SCRIPT
        assert "ANGLE" in ANTI_DETECTION_SCRIPT

    def test_permissions_matches_notification_permission(self) -> None:
        assert "Notification.permission" in ANTI_DETECTION_SCRIPT
        assert "perm === 'default' ? 'prompt' : perm" in ANTI_DETECTION_SCRIPT
