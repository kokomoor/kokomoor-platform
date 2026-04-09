"""Tests for ``core.fetch`` HTTP client and JSON-LD helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
import respx
from bs4 import BeautifulSoup

from core.fetch.browser_fetch import BrowserFetcher
from core.fetch.http_client import HttpFetcher
from core.fetch.jsonld import (
    iter_json_ld_objects_from_html,
    iter_json_ld_objects_from_soup,
    parse_json_ld_payload,
)
from core.fetch.types import FetchMethod

if TYPE_CHECKING:
    from types import TracebackType


class TestParseJsonLdPayload:
    def test_object(self) -> None:
        out = parse_json_ld_payload('{"@type": "Thing", "name": "x"}')
        assert len(out) == 1
        assert out[0] == {"@type": "Thing", "name": "x"}

    def test_array(self) -> None:
        out = parse_json_ld_payload('[1, {"a": 2}]')
        assert out == [1, {"a": 2}]

    def test_invalid_json(self) -> None:
        assert parse_json_ld_payload("{not json") == []


class TestIterJsonLdFromSoup:
    def test_yields_from_scripts(self) -> None:
        html = """
        <html><head>
        <script type="application/ld+json">{"@type": "Thing", "name": "A"}</script>
        <script type="application/ld+json">[{"@type": "Thing", "name": "B"}]</script>
        </head></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        objs = list(iter_json_ld_objects_from_soup(soup))
        assert len(objs) == 2
        assert objs[0]["name"] == "A"
        assert objs[1]["name"] == "B"

    def test_iter_from_html_matches_soup(self) -> None:
        html = '<script type="application/ld+json">{"x": 1}</script>'
        assert list(iter_json_ld_objects_from_html(html)) == [{"x": 1}]


@pytest.mark.asyncio
class TestHttpFetcher:
    async def test_success(self) -> None:
        with respx.mock:
            respx.get("https://example.com/job").mock(
                return_value=httpx.Response(200, text="<html>ok</html>"),
            )
            fetcher = HttpFetcher(max_retries=0)
            result = await fetcher.fetch("https://example.com/job")
            assert result.html == "<html>ok</html>"
            assert "example.com" in result.final_url
            assert result.status_code == 200
            assert result.method == FetchMethod.HTTP

    async def test_retries_then_success(self) -> None:
        with respx.mock:
            route = respx.get("https://example.com/r").mock(
                side_effect=[
                    httpx.ConnectError("fail"),
                    httpx.Response(200, text="ok"),
                ],
            )
            fetcher = HttpFetcher(max_retries=2)
            result = await fetcher.fetch("https://example.com/r")
            assert result.html == "ok"
            assert route.call_count == 2

    async def test_raises_after_exhausted_retries(self) -> None:
        with respx.mock:
            respx.get("https://example.com/fail").mock(side_effect=httpx.ConnectError("nope"))
            fetcher = HttpFetcher(max_retries=1)
            with pytest.raises(ValueError, match="HTTP fetch failed"):
                await fetcher.fetch("https://example.com/fail")


@pytest.mark.asyncio
class TestBrowserFetcher:
    async def test_uses_navigation_response_status_and_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _FakeResponse:
            status = 418

        class _FakePage:
            url = "https://example.com/final"

            async def wait_for_timeout(self, ms: int) -> None:
                return None

            async def content(self) -> str:
                return "<html>ok</html>"

        class _FakeBrowserManager:
            def __init__(self) -> None:
                self.page = _FakePage()
                self.timeout_ms_seen: int | None = None

            async def __aenter__(self) -> _FakeBrowserManager:
                return self

            async def __aexit__(
                self,
                exc_type: type[BaseException] | None,
                exc: BaseException | None,
                tb: TracebackType | None,
            ) -> None:
                return None

            async def new_page(self) -> _FakePage:
                return self.page

            async def rate_limited_goto(
                self,
                page: _FakePage,
                url: str,
                *,
                timeout_ms: int | None,
            ) -> _FakeResponse:
                self.timeout_ms_seen = timeout_ms
                return _FakeResponse()

        fake_manager = _FakeBrowserManager()
        monkeypatch.setattr("core.fetch.browser_fetch.BrowserManager", lambda: fake_manager)

        fetcher = BrowserFetcher(post_wait_ms=0, timeout_ms=12345)
        result = await fetcher.fetch("https://example.com/start")
        assert result.status_code == 418
        assert result.final_url == "https://example.com/final"
        assert fake_manager.timeout_ms_seen == 12345
