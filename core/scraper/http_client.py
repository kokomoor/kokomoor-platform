"""Stealth HTTP client with escalation signals and robots.txt support.

Tries ``httpx`` first with rotated headers. When the response looks like a
block (CAPTCHA page, 403,
CloudFlare challenge), returns an ``EscalationNeeded`` signal so the caller
can retry with a full browser.

This is distinct from ``core.fetch.HttpFetcher`` which is a simpler
utility.  ``StealthHttpClient`` is designed for adversarial scraping
where detection avoidance matters.

Usage::

    client = StealthHttpClient()
    result = await client.get("https://example.com/search?q=test")
    if result.needs_escalation:
        # Retry with Playwright
        ...
"""

from __future__ import annotations

import asyncio
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
import structlog

from core.scraper.path_safety import validate_site_id

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Header pools
# ---------------------------------------------------------------------------

_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

_ACCEPT_LANGUAGES = [
    "en-US,en;q=0.9",
    "en-US,en;q=0.9,es;q=0.8",
    "en-GB,en;q=0.9,en-US;q=0.8",
    "en-US,en;q=0.5",
]

_ACCEPT_ENCODINGS = ["gzip, deflate, br", "gzip, deflate", "gzip, deflate, br, zstd"]

# Patterns that indicate the response is a block, not real content.
_BLOCK_PATTERNS = [
    re.compile(r"captcha", re.IGNORECASE),
    re.compile(r"cf-browser-verification", re.IGNORECASE),
    re.compile(r"challenge-platform", re.IGNORECASE),
    re.compile(r"Access Denied", re.IGNORECASE),
    re.compile(r"please verify you are a human", re.IGNORECASE),
    re.compile(r"blocked.*bot", re.IGNORECASE),
    re.compile(r"<title>\s*Just a moment", re.IGNORECASE),
]


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HttpResult:
    """Outcome of a stealth HTTP request."""

    success: bool
    html: str = ""
    status_code: int | None = None
    final_url: str = ""
    error: str = ""
    needs_escalation: bool = False
    escalation_reason: str = ""
    headers: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Robots.txt cache
# ---------------------------------------------------------------------------


class _RobotsCache:
    """Minimal robots.txt parser with in-memory caching."""

    def __init__(self) -> None:
        self._cache: dict[str, set[str]] = {}
        self._fetch_lock = asyncio.Lock()

    async def is_allowed(self, url: str, client: httpx.AsyncClient) -> bool:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._cache:
            async with self._fetch_lock:
                if origin not in self._cache:
                    await self._fetch(origin, client)
        disallowed = self._cache.get(origin, set())
        return not any(parsed.path.startswith(p) for p in disallowed)

    async def _fetch(self, origin: str, client: httpx.AsyncClient) -> None:
        disallowed: set[str] = set()
        try:
            resp = await client.get(f"{origin}/robots.txt", timeout=httpx.Timeout(10.0))
            if resp.status_code == 200:
                in_wildcard = False
                for line in resp.text.splitlines():
                    line = line.strip()
                    if line.lower().startswith("user-agent:"):
                        agent = line.split(":", 1)[1].strip()
                        in_wildcard = agent == "*"
                    elif in_wildcard and line.lower().startswith("disallow:"):
                        path = line.split(":", 1)[1].strip()
                        if path:
                            disallowed.add(path)
        except Exception:
            pass
        self._cache[origin] = disallowed


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class StealthHttpClient:
    """HTTP client with stealth headers, retry, backoff, and escalation signals."""

    def __init__(
        self,
        *,
        timeout_s: float = 20.0,
        max_retries: int = 2,
        respect_robots: bool = True,
        cookie_dir: str | Path | None = None,
    ) -> None:
        self._timeout = timeout_s
        self._max_retries = max_retries
        self._respect_robots = respect_robots
        self._cookie_dir = Path(cookie_dir or "data/scraper_cookies")
        self._robots = _RobotsCache()

    def _random_headers(self) -> dict[str, str]:
        return {
            "User-Agent": random.choice(_USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": random.choice(_ACCEPT_LANGUAGES),
            "Accept-Encoding": random.choice(_ACCEPT_ENCODINGS),
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
        }

    @staticmethod
    def _redact_url(url: str) -> str:
        parsed = urlparse(url)
        if not parsed.query:
            return url
        redacted_params = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            if key.lower() in {"token", "auth", "session", "key", "password", "sig"}:
                redacted_params.append((key, "***"))
            else:
                redacted_params.append((key, value))
        return urlunparse(parsed._replace(query=urlencode(redacted_params)))

    def _load_cookies(self, site_id: str) -> httpx.Cookies:
        import json

        safe_site_id = validate_site_id(site_id)
        cookie_file = self._cookie_dir / f"{safe_site_id}.json"
        cookies = httpx.Cookies()
        if cookie_file.is_file():
            try:
                data = json.loads(cookie_file.read_text(encoding="utf-8"))
                for name, value in data.items():
                    cookies.set(name, value)
            except (json.JSONDecodeError, OSError):
                pass
        return cookies

    def _save_cookies(self, site_id: str, cookies: httpx.Cookies) -> None:
        import json

        safe_site_id = validate_site_id(site_id)
        self._cookie_dir.mkdir(parents=True, exist_ok=True)
        cookie_file = self._cookie_dir / f"{safe_site_id}.json"
        data = {name: value for name, value in cookies.items()}
        cookie_file.write_text(json.dumps(data), encoding="utf-8")

    @staticmethod
    def _detect_block(html: str, status_code: int) -> str | None:
        if status_code == 403:
            return "HTTP 403 Forbidden"
        if status_code == 429:
            return "HTTP 429 Too Many Requests"
        if status_code >= 500:
            return f"HTTP {status_code} Server Error"
        for pattern in _BLOCK_PATTERNS:
            if pattern.search(html[:5000]):
                return f"Block pattern detected: {pattern.pattern}"
        return None

    async def get(
        self,
        url: str,
        *,
        site_id: str = "",
        override_robots: bool = False,
    ) -> HttpResult:
        """Perform a stealth GET request with retry and block detection."""
        safe_site_id = validate_site_id(site_id) if site_id else ""
        cookies = self._load_cookies(safe_site_id) if safe_site_id else httpx.Cookies()
        headers = self._random_headers()
        last_error = ""

        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(self._timeout),
            cookies=cookies,
        ) as client:
            if self._respect_robots and not override_robots:
                try:
                    allowed = await self._robots.is_allowed(url, client)
                    if not allowed:
                        logger.info("http_client.robots_disallowed", url=url)
                        return HttpResult(
                            success=False,
                            error="Disallowed by robots.txt",
                            needs_escalation=False,
                        )
                except Exception:
                    pass

            for attempt in range(self._max_retries + 1):
                try:
                    resp = await client.get(url, headers=headers)
                    final_url = str(resp.url)
                    html = resp.text

                    block_reason = self._detect_block(html, resp.status_code)
                    if block_reason:
                        logger.warning(
                            "http_client.block_detected",
                            url=self._redact_url(final_url),
                            status=resp.status_code,
                            reason=block_reason,
                        )
                        return HttpResult(
                            success=False,
                            html=html,
                            status_code=resp.status_code,
                            final_url=final_url,
                            needs_escalation=True,
                            escalation_reason=block_reason,
                        )

                    if safe_site_id:
                        self._save_cookies(safe_site_id, client.cookies)

                    resp_headers = dict(resp.headers)
                    logger.info(
                        "http_client.success",
                        url=self._redact_url(final_url),
                        status=resp.status_code,
                        attempt=attempt + 1,
                    )
                    return HttpResult(
                        success=True,
                        html=html,
                        status_code=resp.status_code,
                        final_url=final_url,
                        headers=resp_headers,
                    )
                except httpx.HTTPError as exc:
                    last_error = str(exc)[:300]
                    if attempt < self._max_retries:
                        backoff = (2**attempt) + random.uniform(0.5, 1.5)
                        retry_after = None
                        resp_obj = getattr(exc, "response", None)
                        if resp_obj is not None:
                            retry_after_val = resp_obj.headers.get("Retry-After", "")
                            if retry_after_val and retry_after_val.isdigit():
                                retry_after = int(retry_after_val)
                        wait = retry_after if retry_after else backoff
                        logger.warning(
                            "http_client.retry",
                            url=self._redact_url(url),
                            attempt=attempt + 1,
                            wait_s=round(wait, 1),
                            error=last_error[:160],
                        )
                        await asyncio.sleep(wait)

        return HttpResult(
            success=False,
            error=f"All retries exhausted: {last_error}",
            needs_escalation=True,
            escalation_reason="Network failure after retries",
        )

    async def head(self, url: str) -> HttpResult:
        """Lightweight HEAD request for canary probes."""
        headers = self._random_headers()
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=httpx.Timeout(self._timeout),
            ) as client:
                resp = await client.head(url, headers=headers)
                return HttpResult(
                    success=resp.status_code < 400,
                    status_code=resp.status_code,
                    final_url=str(resp.url),
                    headers=dict(resp.headers),
                )
        except httpx.HTTPError as exc:
            return HttpResult(success=False, error=str(exc)[:300])
