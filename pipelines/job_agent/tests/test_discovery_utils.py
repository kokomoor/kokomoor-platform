"""Tests for discovery subsystem utilities: URL canonicalization, salary parsing,
dedup, scoring, and prefilter.

Pure Python — no external dependencies, no mock browser, no DB.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from pipelines.job_agent.discovery.deduplication import (
    compute_dedup_key,
    deduplicate_refs,
)
from pipelines.job_agent.discovery.models import ListingRef, ParsedSalary, parse_salary_text
from pipelines.job_agent.discovery.prefilter import (
    apply_prefilter,
    score_listing_ref,
)
from pipelines.job_agent.discovery.url_utils import (
    canonicalize_url,
    extract_job_id_from_linkedin_url,
)
from pipelines.job_agent.models import JobSource, SearchCriteria

# ---------------------------------------------------------------------------
# URL canonicalization
# ---------------------------------------------------------------------------


class TestCanonicalizeUrl:
    def test_linkedin_strips_tracking(self) -> None:
        url = "https://www.linkedin.com/jobs/view/1234567/?utm_source=google&trk=abc"
        assert canonicalize_url(url) == "https://www.linkedin.com/jobs/view/1234567/"

    def test_linkedin_normalizes_path(self) -> None:
        url = "https://www.linkedin.com/jobs/1234567/?ref=share"
        assert canonicalize_url(url) == "https://www.linkedin.com/jobs/view/1234567/"

    def test_linkedin_query_param_id(self) -> None:
        url = "https://www.linkedin.com/jobs/search/?currentJobId=6789012&keywords=TPM"
        assert canonicalize_url(url) == "https://www.linkedin.com/jobs/view/6789012/"

    def test_indeed_keeps_jk_only(self) -> None:
        url = "https://www.indeed.com/viewjob?jk=abc123&utm_source=google&from=serp"
        assert canonicalize_url(url) == "https://www.indeed.com/viewjob?jk=abc123"

    def test_indeed_strips_all_tracking(self) -> None:
        url = "https://www.indeed.com/viewjob?clk=1&from=serp"
        assert canonicalize_url(url) == "https://www.indeed.com/viewjob"

    def test_greenhouse_strips_all_query(self) -> None:
        url = "https://boards.greenhouse.io/anduril/jobs/4567?gh_jid=4567&utm_source=x"
        assert canonicalize_url(url) == "https://boards.greenhouse.io/anduril/jobs/4567"

    def test_lever_strips_all_query(self) -> None:
        url = "https://jobs.lever.co/openai/abc-def?lever-via=foo"
        assert canonicalize_url(url) == "https://jobs.lever.co/openai/abc-def"

    def test_generic_keeps_job_params_strips_tracking(self) -> None:
        url = "https://careers.example.com/apply?jobid=789&utm_medium=email&ref=twitter"
        assert canonicalize_url(url) == "https://careers.example.com/apply?jobid=789"

    def test_generic_removes_fragment(self) -> None:
        url = "https://example.com/job/123?id=456#apply-section"
        assert canonicalize_url(url) == "https://example.com/job/123?id=456"

    def test_generic_sorts_params(self) -> None:
        url = "https://example.com/job?token=abc&id=1"
        assert canonicalize_url(url) == "https://example.com/job?id=1&token=abc"


class TestExtractLinkedInJobId:
    def test_view_pattern(self) -> None:
        assert extract_job_id_from_linkedin_url("/jobs/view/1234567/") == "1234567"

    def test_short_pattern(self) -> None:
        assert extract_job_id_from_linkedin_url("/jobs/9999999/?trk=x") == "9999999"

    def test_query_param(self) -> None:
        url = "/jobs/search/?currentJobId=7777777"
        assert extract_job_id_from_linkedin_url(url) == "7777777"

    def test_no_match(self) -> None:
        assert extract_job_id_from_linkedin_url("/company/acme/") is None

    def test_guest_page_slug_url(self) -> None:
        url = "/jobs/view/software-engineer-backend-at-tinder-4328991043?position=2"
        assert extract_job_id_from_linkedin_url(url) == "4328991043"

    def test_guest_page_slug_with_company(self) -> None:
        url = "/jobs/view/senior-swe-at-google-4374834620?position=1&pageNum=0"
        assert extract_job_id_from_linkedin_url(url) == "4374834620"


# ---------------------------------------------------------------------------
# Salary parsing
# ---------------------------------------------------------------------------


class TestParseSalaryText:
    def test_k_range(self) -> None:
        assert parse_salary_text("$180K \u2013 $240K") == ParsedSalary(180_000, 240_000)

    def test_full_range(self) -> None:
        assert parse_salary_text("$180,000 - $240,000") == ParsedSalary(180_000, 240_000)

    def test_k_plus(self) -> None:
        assert parse_salary_text("$180K+") == ParsedSalary(180_000, None)

    def test_up_to(self) -> None:
        assert parse_salary_text("Up to $200K") == ParsedSalary(None, 200_000)

    def test_hourly_skipped(self) -> None:
        assert parse_salary_text("$50/hr") == ParsedSalary(None, None)

    def test_hourly_full_word_skipped(self) -> None:
        assert parse_salary_text("$60/hour") == ParsedSalary(None, None)

    def test_empty_string(self) -> None:
        assert parse_salary_text("") == ParsedSalary(None, None)

    def test_no_match(self) -> None:
        assert parse_salary_text("Competitive salary") == ParsedSalary(None, None)


# ---------------------------------------------------------------------------
# Dedup key
# ---------------------------------------------------------------------------


class TestComputeDedupKey:
    def test_deterministic(self) -> None:
        k1 = compute_dedup_key("Acme", "TPM", "https://example.com/1")
        k2 = compute_dedup_key("Acme", "TPM", "https://example.com/1")
        assert k1 == k2

    def test_case_insensitive(self) -> None:
        k1 = compute_dedup_key("Acme", "TPM", "https://example.com/1")
        k2 = compute_dedup_key("acme", "tpm", "https://example.com/1")
        assert k1 == k2

    def test_different_urls(self) -> None:
        k1 = compute_dedup_key("Acme", "TPM", "https://example.com/1")
        k2 = compute_dedup_key("Acme", "TPM", "https://example.com/2")
        assert k1 != k2

    def test_length_32(self) -> None:
        key = compute_dedup_key("Co", "Title", "https://url.com")
        assert len(key) == 32


# ---------------------------------------------------------------------------
# Deduplication (in-run only, check_db=False)
# ---------------------------------------------------------------------------


class TestDeduplicateRefs:
    @pytest.mark.asyncio
    async def test_removes_in_run_duplicates(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from pipelines.job_agent.discovery.dedup_store import FileDedup

        ref = ListingRef(
            url="https://example.com/1",
            title="TPM",
            company="Acme",
            source=JobSource.LINKEDIN,
        )
        seen: set[str] = set()
        with patch(
            "pipelines.job_agent.discovery.deduplication.FileDedup",
            return_value=FileDedup(tmp_path / "d.json"),
        ):
            result = await deduplicate_refs([ref, ref], in_run_seen=seen, check_db=False)
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_mutates_seen_set(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from pipelines.job_agent.discovery.dedup_store import FileDedup

        ref = ListingRef(
            url="https://example.com/1",
            title="TPM",
            company="Acme",
            source=JobSource.LINKEDIN,
        )
        seen: set[str] = set()
        with patch(
            "pipelines.job_agent.discovery.deduplication.FileDedup",
            return_value=FileDedup(tmp_path / "d.json"),
        ):
            await deduplicate_refs([ref], in_run_seen=seen, check_db=False)
        assert len(seen) == 1

    @pytest.mark.asyncio
    async def test_pre_seen_keys_excluded(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from pipelines.job_agent.discovery.dedup_store import FileDedup

        ref = ListingRef(
            url="https://example.com/1",
            title="TPM",
            company="Acme",
            source=JobSource.LINKEDIN,
        )
        key = compute_dedup_key("Acme", "TPM", "https://example.com/1")
        seen: set[str] = {key}
        with patch(
            "pipelines.job_agent.discovery.deduplication.FileDedup",
            return_value=FileDedup(tmp_path / "d.json"),
        ):
            result = await deduplicate_refs([ref], in_run_seen=seen, check_db=False)
        assert result == []

    @pytest.mark.asyncio
    async def test_distinct_refs_pass(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from pipelines.job_agent.discovery.dedup_store import FileDedup

        ref_a = ListingRef(
            url="https://example.com/a",
            title="PM",
            company="Co1",
            source=JobSource.INDEED,
        )
        ref_b = ListingRef(
            url="https://example.com/b",
            title="SWE",
            company="Co2",
            source=JobSource.INDEED,
        )
        seen: set[str] = set()
        with patch(
            "pipelines.job_agent.discovery.deduplication.FileDedup",
            return_value=FileDedup(tmp_path / "d.json"),
        ):
            result = await deduplicate_refs([ref_a, ref_b], in_run_seen=seen, check_db=False)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

_DEFAULT_CRITERIA = SearchCriteria(
    keywords=["machine learning", "AI"],
    target_companies=["Anduril", "Palantir"],
    target_roles=["TPM", "Product Manager"],
    locations=["San Francisco"],
    remote_ok=True,
)


class TestScoreListingRef:
    def test_role_match(self) -> None:
        ref = ListingRef(url="u", title="Senior TPM", company="Other", source=JobSource.LINKEDIN)
        score = score_listing_ref(ref, _DEFAULT_CRITERIA)
        assert score >= 0.40

    def test_keyword_match(self) -> None:
        ref = ListingRef(
            url="u",
            title="Machine Learning Engineer",
            company="Other",
            source=JobSource.LINKEDIN,
        )
        score = score_listing_ref(ref, _DEFAULT_CRITERIA)
        assert score >= 0.10

    def test_company_match(self) -> None:
        ref = ListingRef(url="u", title="Analyst", company="Anduril", source=JobSource.LINKEDIN)
        score = score_listing_ref(ref, _DEFAULT_CRITERIA)
        assert score >= 0.35

    def test_location_match(self) -> None:
        ref = ListingRef(
            url="u",
            title="Ops",
            company="Other",
            source=JobSource.LINKEDIN,
            location="San Francisco, CA",
        )
        score = score_listing_ref(ref, _DEFAULT_CRITERIA)
        assert score >= 0.10

    def test_remote_match(self) -> None:
        ref = ListingRef(
            url="u",
            title="Ops",
            company="Other",
            source=JobSource.LINKEDIN,
            location="Remote",
        )
        score = score_listing_ref(ref, _DEFAULT_CRITERIA)
        assert score >= 0.10

    def test_disqualifier_reduces_score(self) -> None:
        ref = ListingRef(
            url="u",
            title="TPM Intern",
            company="Anduril",
            source=JobSource.LINKEDIN,
        )
        score = score_listing_ref(ref, _DEFAULT_CRITERIA)
        assert score < 0.40

    def test_no_match_scores_zero(self) -> None:
        ref = ListingRef(url="u", title="Nurse", company="Hospital", source=JobSource.OTHER)
        assert score_listing_ref(ref, _DEFAULT_CRITERIA) == 0.0

    def test_combined_high_score(self) -> None:
        ref = ListingRef(
            url="u",
            title="Senior TPM - AI Platform",
            company="Anduril",
            source=JobSource.LINKEDIN,
            location="Remote",
        )
        score = score_listing_ref(ref, _DEFAULT_CRITERIA)
        assert score >= 0.85


# ---------------------------------------------------------------------------
# Prefilter
# ---------------------------------------------------------------------------


class TestApplyPrefilter:
    def test_min_zero_passes_all(self) -> None:
        refs = [
            ListingRef(url="u", title="Nurse", company="Hospital", source=JobSource.OTHER),
            ListingRef(url="u2", title="TPM", company="Anduril", source=JobSource.LINKEDIN),
        ]
        passed, rejected = apply_prefilter(refs, _DEFAULT_CRITERIA, min_score=0.0)
        assert len(passed) == 2
        assert rejected == []

    def test_threshold_filters(self) -> None:
        refs = [
            ListingRef(url="u", title="Nurse", company="Hospital", source=JobSource.OTHER),
            ListingRef(url="u2", title="Senior TPM", company="Anduril", source=JobSource.LINKEDIN),
        ]
        passed, rejected = apply_prefilter(refs, _DEFAULT_CRITERIA, min_score=0.5)
        assert len(passed) == 1
        assert passed[0].title == "Senior TPM"
        assert len(rejected) == 1
