"""Tests for Greenhouse and Lever HTTP providers."""

from __future__ import annotations

import httpx
import pytest
import respx

from pipelines.job_agent.discovery.models import DiscoveryConfig
from pipelines.job_agent.discovery.providers.greenhouse import (
    GreenhouseProvider,
    fetch_all_greenhouse_companies,
)
from pipelines.job_agent.discovery.providers.lever import (
    LeverProvider,
    fetch_all_lever_companies,
)
from pipelines.job_agent.models import JobSource, SearchCriteria

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG = DiscoveryConfig(
    sessions_dir="/tmp/test-sessions",
    max_listings_per_provider=150,
)

_CRITERIA_WITH_KEYWORDS = SearchCriteria(
    keywords=["engineer"],
    target_roles=["Software Engineer"],
)

_CRITERIA_EMPTY = SearchCriteria()


# ---------------------------------------------------------------------------
# Greenhouse
# ---------------------------------------------------------------------------


class TestGreenhouseProvider:
    @respx.mock
    @pytest.mark.asyncio
    async def test_valid_response_returns_refs(self) -> None:
        payload = {
            "jobs": [
                {
                    "id": 100,
                    "title": "Software Engineer",
                    "absolute_url": "https://boards.greenhouse.io/acme/jobs/100",
                    "location": {"name": "San Francisco, CA"},
                },
                {
                    "id": 101,
                    "title": "Product Designer",
                    "absolute_url": "https://boards.greenhouse.io/acme/jobs/101",
                    "location": {"name": "Remote"},
                },
            ]
        }
        respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=false").mock(
            return_value=httpx.Response(200, json=payload)
        )

        provider = GreenhouseProvider("acme")
        refs = await provider.run_search(
            None,  # type: ignore[arg-type]
            _CRITERIA_WITH_KEYWORDS,
            _DEFAULT_CONFIG,
            behavior=None,  # type: ignore[arg-type]
            rate_limiter=None,  # type: ignore[arg-type]
            captcha_handler=None,  # type: ignore[arg-type]
        )

        assert len(refs) == 1
        assert refs[0].title == "Software Engineer"
        assert refs[0].company == "Acme"
        assert refs[0].source == JobSource.GREENHOUSE
        assert refs[0].location == "San Francisco, CA"
        assert "greenhouse.io" in refs[0].url

    @respx.mock
    @pytest.mark.asyncio
    async def test_empty_criteria_returns_all(self) -> None:
        payload = {
            "jobs": [
                {
                    "id": 200,
                    "title": "Data Analyst",
                    "absolute_url": "https://boards.greenhouse.io/acme/jobs/200",
                    "location": {"name": "NYC"},
                },
                {
                    "id": 201,
                    "title": "Chef",
                    "absolute_url": "https://boards.greenhouse.io/acme/jobs/201",
                    "location": {"name": "LA"},
                },
            ]
        }
        respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=false").mock(
            return_value=httpx.Response(200, json=payload)
        )

        refs = await GreenhouseProvider("acme").run_search(
            None,  # type: ignore[arg-type]
            _CRITERIA_EMPTY,
            _DEFAULT_CONFIG,
            behavior=None,  # type: ignore[arg-type]
            rate_limiter=None,  # type: ignore[arg-type]
            captcha_handler=None,  # type: ignore[arg-type]
        )
        assert len(refs) == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_empty_jobs_returns_empty(self) -> None:
        respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=false").mock(
            return_value=httpx.Response(200, json={"jobs": []})
        )

        refs = await GreenhouseProvider("acme").run_search(
            None,  # type: ignore[arg-type]
            _CRITERIA_EMPTY,
            _DEFAULT_CONFIG,
            behavior=None,  # type: ignore[arg-type]
            rate_limiter=None,  # type: ignore[arg-type]
            captcha_handler=None,  # type: ignore[arg-type]
        )
        assert refs == []

    @respx.mock
    @pytest.mark.asyncio
    async def test_network_error_returns_empty(self) -> None:
        respx.get("https://boards-api.greenhouse.io/v1/boards/broken/jobs?content=false").mock(
            side_effect=httpx.ConnectError("connection refused")
        )

        refs = await GreenhouseProvider("broken").run_search(
            None,  # type: ignore[arg-type]
            _CRITERIA_EMPTY,
            _DEFAULT_CONFIG,
            behavior=None,  # type: ignore[arg-type]
            rate_limiter=None,  # type: ignore[arg-type]
            captcha_handler=None,  # type: ignore[arg-type]
        )
        assert refs == []

    @respx.mock
    @pytest.mark.asyncio
    async def test_title_filter_excludes_non_matching(self) -> None:
        payload = {
            "jobs": [
                {
                    "id": 300,
                    "title": "Marketing Manager",
                    "absolute_url": "https://boards.greenhouse.io/acme/jobs/300",
                    "location": {"name": "Remote"},
                },
            ]
        }
        respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=false").mock(
            return_value=httpx.Response(200, json=payload)
        )

        refs = await GreenhouseProvider("acme").run_search(
            None,  # type: ignore[arg-type]
            _CRITERIA_WITH_KEYWORDS,
            _DEFAULT_CONFIG,
            behavior=None,  # type: ignore[arg-type]
            rate_limiter=None,  # type: ignore[arg-type]
            captcha_handler=None,  # type: ignore[arg-type]
        )
        assert refs == []

    @respx.mock
    @pytest.mark.asyncio
    async def test_http_404_returns_empty(self) -> None:
        respx.get("https://boards-api.greenhouse.io/v1/boards/gone/jobs?content=false").mock(
            return_value=httpx.Response(404, text="Not Found")
        )

        refs = await GreenhouseProvider("gone").run_search(
            None,  # type: ignore[arg-type]
            _CRITERIA_EMPTY,
            _DEFAULT_CONFIG,
            behavior=None,  # type: ignore[arg-type]
            rate_limiter=None,  # type: ignore[arg-type]
            captcha_handler=None,  # type: ignore[arg-type]
        )
        assert refs == []

    @respx.mock
    @pytest.mark.asyncio
    async def test_malformed_json_returns_empty(self) -> None:
        respx.get("https://boards-api.greenhouse.io/v1/boards/bad/jobs?content=false").mock(
            return_value=httpx.Response(200, text="not json at all{{{")
        )

        refs = await GreenhouseProvider("bad").run_search(
            None,  # type: ignore[arg-type]
            _CRITERIA_EMPTY,
            _DEFAULT_CONFIG,
            behavior=None,  # type: ignore[arg-type]
            rate_limiter=None,  # type: ignore[arg-type]
            captcha_handler=None,  # type: ignore[arg-type]
        )
        assert refs == []

    @respx.mock
    @pytest.mark.asyncio
    async def test_respects_max_listings_cap(self) -> None:
        jobs = [
            {
                "id": i,
                "title": f"Software Engineer {i}",
                "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{i}",
                "location": {"name": "Remote"},
            }
            for i in range(20)
        ]
        respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=false").mock(
            return_value=httpx.Response(200, json={"jobs": jobs})
        )

        config = DiscoveryConfig(sessions_dir="/tmp/test", max_listings_per_provider=5)
        refs = await GreenhouseProvider("acme").run_search(
            None,  # type: ignore[arg-type]
            _CRITERIA_WITH_KEYWORDS,
            config,
            behavior=None,  # type: ignore[arg-type]
            rate_limiter=None,  # type: ignore[arg-type]
            captcha_handler=None,  # type: ignore[arg-type]
        )
        assert len(refs) == 5

    @respx.mock
    @pytest.mark.asyncio
    async def test_url_is_canonicalized(self) -> None:
        payload = {
            "jobs": [
                {
                    "id": 400,
                    "title": "Engineer",
                    "absolute_url": "https://boards.greenhouse.io/acme/jobs/400?utm_source=x#apply",
                    "location": {"name": "Remote"},
                },
            ]
        }
        respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=false").mock(
            return_value=httpx.Response(200, json=payload)
        )

        refs = await GreenhouseProvider("acme").run_search(
            None,  # type: ignore[arg-type]
            _CRITERIA_EMPTY,
            _DEFAULT_CONFIG,
            behavior=None,  # type: ignore[arg-type]
            rate_limiter=None,  # type: ignore[arg-type]
            captcha_handler=None,  # type: ignore[arg-type]
        )
        assert len(refs) == 1
        assert "utm_source" not in refs[0].url
        assert "#" not in refs[0].url


class TestFetchAllGreenhouseCompanies:
    @respx.mock
    @pytest.mark.asyncio
    async def test_partial_failure_returns_successful_results(self) -> None:
        respx.get("https://boards-api.greenhouse.io/v1/boards/good-co/jobs?content=false").mock(
            return_value=httpx.Response(
                200,
                json={
                    "jobs": [
                        {
                            "id": 1,
                            "title": "Engineer",
                            "absolute_url": "https://boards.greenhouse.io/good-co/jobs/1",
                            "location": {"name": "Remote"},
                        }
                    ]
                },
            )
        )
        respx.get("https://boards-api.greenhouse.io/v1/boards/bad-co/jobs?content=false").mock(
            side_effect=httpx.ConnectError("down")
        )

        refs = await fetch_all_greenhouse_companies(
            ["good-co", "bad-co"], _CRITERIA_EMPTY, _DEFAULT_CONFIG
        )
        assert len(refs) == 1
        assert refs[0].company == "Good Co"


# ---------------------------------------------------------------------------
# Lever
# ---------------------------------------------------------------------------


class TestLeverProvider:
    @respx.mock
    @pytest.mark.asyncio
    async def test_valid_response_returns_refs(self) -> None:
        postings = [
            {
                "id": "abc",
                "text": "Backend Engineer",
                "hostedUrl": "https://jobs.lever.co/acme/abc",
                "categories": {"location": "New York, NY"},
            },
            {
                "id": "def",
                "text": "Office Manager",
                "hostedUrl": "https://jobs.lever.co/acme/def",
                "categories": {"location": "Austin, TX"},
            },
        ]
        respx.get("https://api.lever.co/v0/postings/acme?mode=json").mock(
            return_value=httpx.Response(200, json=postings)
        )

        refs = await LeverProvider("acme").run_search(
            None,  # type: ignore[arg-type]
            _CRITERIA_WITH_KEYWORDS,
            _DEFAULT_CONFIG,
            behavior=None,  # type: ignore[arg-type]
            rate_limiter=None,  # type: ignore[arg-type]
            captcha_handler=None,  # type: ignore[arg-type]
        )
        assert len(refs) == 1
        assert refs[0].title == "Backend Engineer"
        assert refs[0].source == JobSource.LEVER
        assert refs[0].location == "New York, NY"
        assert "lever.co" in refs[0].url

    @respx.mock
    @pytest.mark.asyncio
    async def test_empty_criteria_returns_all(self) -> None:
        postings = [
            {
                "id": "a",
                "text": "Janitor",
                "hostedUrl": "https://jobs.lever.co/co/a",
                "categories": {"location": "SF"},
            },
        ]
        respx.get("https://api.lever.co/v0/postings/co?mode=json").mock(
            return_value=httpx.Response(200, json=postings)
        )

        refs = await LeverProvider("co").run_search(
            None,  # type: ignore[arg-type]
            _CRITERIA_EMPTY,
            _DEFAULT_CONFIG,
            behavior=None,  # type: ignore[arg-type]
            rate_limiter=None,  # type: ignore[arg-type]
            captcha_handler=None,  # type: ignore[arg-type]
        )
        assert len(refs) == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_empty_response_returns_empty(self) -> None:
        respx.get("https://api.lever.co/v0/postings/empty?mode=json").mock(
            return_value=httpx.Response(200, json=[])
        )

        refs = await LeverProvider("empty").run_search(
            None,  # type: ignore[arg-type]
            _CRITERIA_EMPTY,
            _DEFAULT_CONFIG,
            behavior=None,  # type: ignore[arg-type]
            rate_limiter=None,  # type: ignore[arg-type]
            captcha_handler=None,  # type: ignore[arg-type]
        )
        assert refs == []

    @respx.mock
    @pytest.mark.asyncio
    async def test_network_error_returns_empty(self) -> None:
        respx.get("https://api.lever.co/v0/postings/broken?mode=json").mock(
            side_effect=httpx.ConnectError("timeout")
        )

        refs = await LeverProvider("broken").run_search(
            None,  # type: ignore[arg-type]
            _CRITERIA_EMPTY,
            _DEFAULT_CONFIG,
            behavior=None,  # type: ignore[arg-type]
            rate_limiter=None,  # type: ignore[arg-type]
            captcha_handler=None,  # type: ignore[arg-type]
        )
        assert refs == []

    @respx.mock
    @pytest.mark.asyncio
    async def test_title_filter_excludes_non_matching(self) -> None:
        postings = [
            {
                "id": "x",
                "text": "HR Coordinator",
                "hostedUrl": "https://jobs.lever.co/co/x",
                "categories": {"location": "Remote"},
            },
        ]
        respx.get("https://api.lever.co/v0/postings/co?mode=json").mock(
            return_value=httpx.Response(200, json=postings)
        )

        refs = await LeverProvider("co").run_search(
            None,  # type: ignore[arg-type]
            _CRITERIA_WITH_KEYWORDS,
            _DEFAULT_CONFIG,
            behavior=None,  # type: ignore[arg-type]
            rate_limiter=None,  # type: ignore[arg-type]
            captcha_handler=None,  # type: ignore[arg-type]
        )
        assert refs == []

    @respx.mock
    @pytest.mark.asyncio
    async def test_respects_max_listings_cap(self) -> None:
        postings = [
            {
                "id": str(i),
                "text": f"Software Engineer {i}",
                "hostedUrl": f"https://jobs.lever.co/acme/{i}",
                "categories": {"location": "Remote"},
            }
            for i in range(20)
        ]
        respx.get("https://api.lever.co/v0/postings/acme?mode=json").mock(
            return_value=httpx.Response(200, json=postings)
        )

        config = DiscoveryConfig(sessions_dir="/tmp/test", max_listings_per_provider=5)
        refs = await LeverProvider("acme").run_search(
            None,  # type: ignore[arg-type]
            _CRITERIA_WITH_KEYWORDS,
            config,
            behavior=None,  # type: ignore[arg-type]
            rate_limiter=None,  # type: ignore[arg-type]
            captcha_handler=None,  # type: ignore[arg-type]
        )
        assert len(refs) == 5

    @respx.mock
    @pytest.mark.asyncio
    async def test_url_is_canonicalized(self) -> None:
        postings = [
            {
                "id": "z",
                "text": "Engineer",
                "hostedUrl": "https://jobs.lever.co/acme/z?utm_source=email#apply",
                "categories": {"location": "Remote"},
            },
        ]
        respx.get("https://api.lever.co/v0/postings/acme?mode=json").mock(
            return_value=httpx.Response(200, json=postings)
        )

        refs = await LeverProvider("acme").run_search(
            None,  # type: ignore[arg-type]
            _CRITERIA_EMPTY,
            _DEFAULT_CONFIG,
            behavior=None,  # type: ignore[arg-type]
            rate_limiter=None,  # type: ignore[arg-type]
            captcha_handler=None,  # type: ignore[arg-type]
        )
        assert len(refs) == 1
        assert "utm_source" not in refs[0].url
        assert "#" not in refs[0].url

    @respx.mock
    @pytest.mark.asyncio
    async def test_non_list_response_returns_empty(self) -> None:
        respx.get("https://api.lever.co/v0/postings/weird?mode=json").mock(
            return_value=httpx.Response(200, json={"error": "not found"})
        )

        refs = await LeverProvider("weird").run_search(
            None,  # type: ignore[arg-type]
            _CRITERIA_EMPTY,
            _DEFAULT_CONFIG,
            behavior=None,  # type: ignore[arg-type]
            rate_limiter=None,  # type: ignore[arg-type]
            captcha_handler=None,  # type: ignore[arg-type]
        )
        assert refs == []


class TestFetchAllLeverCompanies:
    @respx.mock
    @pytest.mark.asyncio
    async def test_partial_failure_returns_successful_results(self) -> None:
        respx.get("https://api.lever.co/v0/postings/good-co?mode=json").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "id": "a1",
                        "text": "Engineer",
                        "hostedUrl": "https://jobs.lever.co/good-co/a1",
                        "categories": {"location": "Remote"},
                    }
                ],
            )
        )
        respx.get("https://api.lever.co/v0/postings/bad-co?mode=json").mock(
            side_effect=httpx.ConnectError("down")
        )

        refs = await fetch_all_lever_companies(
            ["good-co", "bad-co"], _CRITERIA_EMPTY, _DEFAULT_CONFIG
        )
        assert len(refs) == 1
        assert refs[0].company == "Good Co"
