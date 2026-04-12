"""Tests for discovery prefilter scoring and filtering."""

from __future__ import annotations

from pipelines.job_agent.discovery.models import ListingRef
from pipelines.job_agent.discovery.prefilter import apply_prefilter, score_listing_ref
from pipelines.job_agent.models import JobSource, SearchCriteria

_CRITERIA = SearchCriteria(
    keywords=["python", "backend"],
    target_companies=["Anduril", "Palantir"],
    target_roles=["Software Engineer", "Staff Engineer"],
    locations=["San Francisco"],
    remote_ok=True,
)


class TestScoreListingRef:
    def test_role_match_gives_040(self) -> None:
        ref = ListingRef(
            url="u", title="Senior Software Engineer", company="Other", source=JobSource.OTHER
        )
        assert score_listing_ref(ref, _CRITERIA) >= 0.40

    def test_disqualifier_clamps_to_zero(self) -> None:
        ref = ListingRef(url="u", title="Software Intern", company="Other", source=JobSource.OTHER)
        assert score_listing_ref(ref, _CRITERIA) <= 0.0

    def test_internship_disqualifier(self) -> None:
        ref = ListingRef(
            url="u", title="Internship Program", company="Other", source=JobSource.OTHER
        )
        assert score_listing_ref(ref, _CRITERIA) == 0.0

    def test_company_match_gives_035(self) -> None:
        ref = ListingRef(url="u", title="Random Title", company="Anduril", source=JobSource.OTHER)
        assert score_listing_ref(ref, _CRITERIA) >= 0.35

    def test_location_match(self) -> None:
        ref = ListingRef(
            url="u",
            title="Ops",
            company="Other",
            source=JobSource.OTHER,
            location="San Francisco, CA",
        )
        assert score_listing_ref(ref, _CRITERIA) >= 0.10

    def test_remote_ok_matches(self) -> None:
        ref = ListingRef(
            url="u",
            title="Ops",
            company="Other",
            source=JobSource.OTHER,
            location="Remote",
        )
        assert score_listing_ref(ref, _CRITERIA) >= 0.10

    def test_combined_role_and_company(self) -> None:
        ref = ListingRef(
            url="u",
            title="Staff Engineer",
            company="Palantir",
            source=JobSource.OTHER,
            location="Remote",
        )
        assert score_listing_ref(ref, _CRITERIA) >= 0.75

    def test_empty_criteria_scores_zero(self) -> None:
        ref = ListingRef(url="u", title="Anything", company="Anything", source=JobSource.OTHER)
        assert score_listing_ref(ref, SearchCriteria()) == 0.0

    def test_keyword_match_adds_score(self) -> None:
        ref = ListingRef(
            url="u",
            title="Python Backend Developer",
            company="Other",
            source=JobSource.OTHER,
        )
        score = score_listing_ref(ref, _CRITERIA)
        assert score >= 0.20


class TestApplyPrefilter:
    def test_min_score_zero_passes_all(self) -> None:
        refs = [
            ListingRef(url="u1", title="Nurse", company="Hospital", source=JobSource.OTHER),
            ListingRef(url="u2", title="Staff Engineer", company="Anduril", source=JobSource.OTHER),
        ]
        passed, rejected = apply_prefilter(refs, _CRITERIA, min_score=0.0)
        assert len(passed) == 2
        assert len(rejected) == 0

    def test_high_threshold_filters_low_score(self) -> None:
        refs = [
            ListingRef(url="u1", title="Nurse", company="Hospital", source=JobSource.OTHER),
            ListingRef(url="u2", title="Staff Engineer", company="Anduril", source=JobSource.OTHER),
        ]
        passed, rejected = apply_prefilter(refs, _CRITERIA, min_score=0.5)
        assert len(passed) == 1
        assert passed[0].title == "Staff Engineer"
        assert len(rejected) == 1

    def test_empty_refs_returns_empty(self) -> None:
        passed, rejected = apply_prefilter([], _CRITERIA, min_score=0.5)
        assert passed == []
        assert rejected == []

    def test_empty_criteria_min_zero_passes_all(self) -> None:
        refs = [
            ListingRef(url="u", title="Anything", company="Any", source=JobSource.OTHER),
        ]
        passed, _rejected = apply_prefilter(refs, SearchCriteria(), min_score=0.0)
        assert len(passed) == 1
