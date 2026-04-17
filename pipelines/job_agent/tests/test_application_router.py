"""Tests for ATS detection and application strategy routing."""

from __future__ import annotations

import pytest

from pipelines.job_agent.application.router import (
    SubmissionStrategy,
    detect_ats_platform,
    route_application,
)
from pipelines.job_agent.models import JobListing


def _listing(url: str) -> JobListing:
    return JobListing(
        title="Software Engineer",
        company="Example Co",
        location="Remote",
        url=url,
        dedup_key=f"dedup::{url}",
    )


@pytest.mark.parametrize(
    ("platform", "url"),
    [
        ("greenhouse", "https://boards.greenhouse.io/acme/jobs/12345"),
        ("greenhouse", "http://job-boards.greenhouse.io/acme/jobs/12345?gh_jid=12345"),
        ("lever", "https://jobs.lever.co/acme/123e4567-e89b-12d3-a456-426614174000"),
        ("lever", "https://acme.lever.co/jobs/123e4567-e89b-12d3-a456-426614174000/"),
        (
            "workday",
            "https://acme.wd5.myworkdayjobs.com/en-US/External/job/Boston-MA/Engineer_R123",
        ),
        ("workday", "https://myworkdayjobs.com/acme/careers/job/Engineer_R123"),
        ("icims", "https://jobs.icims.com/jobs/1234/software-engineer/job"),
        ("icims", "https://careers-acme.icims.com/jobs/search?ss=1"),
        ("taleo", "https://acme.taleo.net/careersection/2/jobdetail.ftl?job=12345"),
        ("taleo", "https://taleo.net/careersection/jobdetail.ftl?job=12345"),
        ("ashby", "https://jobs.ashbyhq.com/acme/1234-abcd"),
        ("ashby", "https://acme.ashbyhq.com/job/1234"),
        (
            "smartrecruiters",
            "https://jobs.smartrecruiters.com/Acme/743999999999999-engineer",
        ),
        (
            "smartrecruiters",
            "https://www.smartrecruiters.com/Acme/743999999999999-engineer",
        ),
        ("bamboohr", "https://acme.bamboohr.com/careers/42"),
        ("bamboohr", "https://bamboohr.com/careers/42"),
        (
            "linkedin",
            "https://www.linkedin.com/jobs/view/software-engineer-at-acme-1234567890/",
        ),
        ("linkedin", "https://linkedin.com/jobs/search/?currentJobId=1234567890"),
    ],
)
def test_detect_ats_platform_variants(platform: str, url: str) -> None:
    assert detect_ats_platform(url) == platform


@pytest.mark.parametrize(
    ("url", "strategy", "requires_browser", "requires_account", "ats_platform"),
    [
        (
            "https://boards.greenhouse.io/acme/jobs/12345",
            SubmissionStrategy.API_GREENHOUSE,
            False,
            False,
            "greenhouse",
        ),
        (
            "https://jobs.lever.co/acme/123e4567-e89b-12d3-a456-426614174000",
            SubmissionStrategy.API_LEVER,
            False,
            False,
            "lever",
        ),
        (
            "https://www.linkedin.com/jobs/view/software-engineer-at-acme-1234567890/",
            SubmissionStrategy.TEMPLATE_LINKEDIN_EASY_APPLY,
            True,
            True,
            "linkedin",
        ),
        (
            "https://jobs.ashbyhq.com/acme/1234-abcd",
            SubmissionStrategy.TEMPLATE_ASHBY,
            True,
            False,
            "ashby",
        ),
        (
            "https://acme.wd5.myworkdayjobs.com/en-US/External/job/Boston-MA/Engineer_R123",
            SubmissionStrategy.AGENT_WORKDAY,
            True,
            True,
            "workday",
        ),
        (
            "https://jobs.icims.com/jobs/1234/software-engineer/job",
            SubmissionStrategy.AGENT_GENERIC,
            True,
            True,
            "icims",
        ),
        (
            "https://acme.taleo.net/careersection/2/jobdetail.ftl?job=12345",
            SubmissionStrategy.AGENT_GENERIC,
            True,
            True,
            "taleo",
        ),
        (
            "https://jobs.smartrecruiters.com/Acme/743999999999999-engineer",
            SubmissionStrategy.AGENT_GENERIC,
            True,
            False,
            "smartrecruiters",
        ),
        (
            "https://acme.bamboohr.com/careers/42",
            SubmissionStrategy.AGENT_GENERIC,
            True,
            False,
            "bamboohr",
        ),
    ],
)
async def test_route_application_expected_strategy(
    url: str,
    strategy: SubmissionStrategy,
    requires_browser: bool,
    requires_account: bool,
    ats_platform: str,
) -> None:
    decision = await route_application(_listing(url))
    assert decision.strategy == strategy
    assert decision.application_url == url
    assert decision.requires_browser is requires_browser
    assert decision.requires_account is requires_account
    assert decision.ats_platform == ats_platform


async def test_route_application_unknown_domain_defaults_to_generic() -> None:
    url = "https://careers.example.com/jobs/software-engineer"
    decision = await route_application(_listing(url))
    assert decision.strategy == SubmissionStrategy.AGENT_GENERIC
    assert decision.ats_platform == "unknown"
    assert decision.requires_browser is True


@pytest.mark.parametrize("url", ["", "invalid-domain", "not a url"])
async def test_route_application_garbage_url_defaults_to_generic(url: str) -> None:
    decision = await route_application(_listing(url))
    assert decision.strategy == SubmissionStrategy.AGENT_GENERIC
    assert decision.ats_platform == "unknown"
