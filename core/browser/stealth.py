"""Anti-detection defaults for Playwright browser contexts.

Generates randomised but realistic browser fingerprints to reduce the
likelihood of bot detection on protected sites.
This is not about being adversarial — it's about not looking like a
headless Chrome instance running on a Ryzen server.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from playwright.async_api import Page

# Realistic desktop user agents (rotate per session).
_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
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
        "screen": {"width": viewport["width"] + 120, "height": viewport["height"] + 120},
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


# ---------------------------------------------------------------------------
# Page-level anti-detection script
# ---------------------------------------------------------------------------
# Injected via page.add_init_script() so it runs before any page JS,
# including bot-detection libraries. Covers vectors that context-level
# settings (UA, viewport, timezone, locale) cannot address.
# ---------------------------------------------------------------------------

ANTI_DETECTION_SCRIPT: str = """
(() => {
  // 1. Hide navigator.webdriver flag — real Chrome returns false, not undefined
  Object.defineProperty(navigator, 'webdriver', {
    get: () => false,
    configurable: true,
  });

  // 2. Fake navigator.plugins with realistic Chrome entries
  Object.defineProperty(navigator, 'plugins', {
    get: () => {
      const makePlugin = (name, filename) => {
        const p = Object.create(Plugin.prototype);
        Object.defineProperty(p, 'name', { get: () => name });
        Object.defineProperty(p, 'filename', { get: () => filename });
        Object.defineProperty(p, 'length', { get: () => 0 });
        return p;
      };
      const list = [
        makePlugin('Chrome PDF Plugin', 'internal-pdf-viewer'),
        makePlugin('Chrome PDF Viewer', 'mhjfbmdgcfjbbpaeojofohoefgiehjai'),
      ];
      list.item = (i) => list[i] || null;
      list.namedItem = (n) => list.find(p => p.name === n) || null;
      list.refresh = () => {};
      return list;
    },
    configurable: false,
  });

  // 2b. Provide chrome.runtime surface expected by anti-bot checks
  if (!window.chrome) {
    Object.defineProperty(window, 'chrome', {
      value: {},
      configurable: false,
      enumerable: true,
      writable: false,
    });
  }
  if (!window.chrome.runtime) {
    const noop = () => {};
    const noopPort = { onMessage: { addListener: noop, removeListener: noop }, postMessage: noop, disconnect: noop };
    Object.defineProperty(window.chrome, 'runtime', {
      value: {
        connect: function() { return noopPort; },
        sendMessage: function() {},
        id: undefined,
      },
      configurable: false,
      enumerable: true,
      writable: false,
    });
  }

  // 3. WebGL vendor/renderer spoofing — platform-matched to UA for coherence
  const ua = navigator.userAgent;
  let _glVendor = 'Intel Inc.';
  let _glRenderer = 'Intel Iris OpenGL Engine';
  if (/Windows/.test(ua)) {
    _glVendor = 'Google Inc. (Intel)';
    _glRenderer = 'ANGLE (Intel, Intel(R) UHD Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)';
  } else if (/Linux/.test(ua)) {
    _glVendor = 'Mesa';
    _glRenderer = 'Mesa Intel(R) UHD Graphics 630 (CFL GT2)';
  }

  const origGetParameter = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function(param) {
    if (param === 37445) return _glVendor;
    if (param === 37446) return _glRenderer;
    return origGetParameter.call(this, param);
  };
  if (typeof WebGL2RenderingContext !== 'undefined') {
    const origGetParameter2 = WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter = function(param) {
      if (param === 37445) return _glVendor;
      if (param === 37446) return _glRenderer;
      return origGetParameter2.call(this, param);
    };
  }

  // 4. Canvas fingerprint noise — low-amplitude random jitter per call
  const randByte = () => {
    try {
      const arr = new Uint8Array(1);
      self.crypto.getRandomValues(arr);
      return arr[0];
    } catch (e) {
      return Math.floor(Math.random() * 256);
    }
  };
  const channelNoise = () => (randByte() % 3) + 1;
  const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
  HTMLCanvasElement.prototype.toDataURL = function(...args) {
    try {
      const ctx = this.getContext('2d');
      if (ctx) {
        const pixel = ctx.getImageData(0, 0, 1, 1);
        pixel.data[0] = (pixel.data[0] + channelNoise()) % 256;
        pixel.data[1] = (pixel.data[1] + channelNoise()) % 256;
        pixel.data[2] = (pixel.data[2] + channelNoise()) % 256;
        pixel.data[3] = 255;
        ctx.putImageData(pixel, 0, 0);
      }
    } catch (e) { /* canvas may be tainted or WebGL-only */ }
    return origToDataURL.apply(this, args);
  };

  // 5. AudioContext fingerprint — add subtle noise to frequency analysis
  const origCreateOscillator = AudioContext.prototype.createOscillator;
  AudioContext.prototype.createOscillator = function() {
    const osc = origCreateOscillator.call(this);
    const origFreq = osc.frequency.value;
    osc.frequency.value = origFreq + (Math.random() * 0.01 - 0.005);
    return osc;
  };

  // 6. Font enumeration defense — report a standard set to frustrate fingerprinting
  if (document.fonts && document.fonts.check) {
    const origCheck = document.fonts.check.bind(document.fonts);
    const alwaysAvailable = new Set([
      'Arial', 'Verdana', 'Helvetica', 'Times New Roman', 'Georgia',
      'Courier New', 'Comic Sans MS', 'Trebuchet MS', 'Impact',
    ]);
    document.fonts.check = function(font, text) {
      const fontName = font.replace(/["']/g, '').split(',')[0].trim();
      if (alwaysAvailable.has(fontName)) return true;
      return origCheck(font, text);
    };
  }

  // 7. Permissions API — deny fingerprinting via permissions query
  if (navigator.permissions && navigator.permissions.query) {
    const origQuery = navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query = function(desc) {
      if (desc.name === 'notifications') {
        return Promise.resolve({ state: 'prompt', onchange: null });
      }
      return origQuery(desc);
    };
  }

  // 8. WebRTC leak prevention — prevent IP leaking via RTCPeerConnection
  if (typeof RTCPeerConnection !== 'undefined') {
    const origRTC = RTCPeerConnection;
    window.RTCPeerConnection = function(config, constraints) {
      if (config && config.iceServers) {
        config.iceServers = [];
      }
      return new origRTC(config, constraints);
    };
    window.RTCPeerConnection.prototype = origRTC.prototype;
  }

})();
"""


async def apply_page_stealth(page: Page) -> None:
    """Register anti-detection JS to run before any page script.

    Must be called before navigation so the init script is active
    at document creation time.
    """
    await page.add_init_script(ANTI_DETECTION_SCRIPT)
