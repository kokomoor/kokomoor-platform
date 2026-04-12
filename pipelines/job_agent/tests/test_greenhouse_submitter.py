"""Tests for the Greenhouse Job Board API submitter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from pipelines.job_agent.application.submitters.greenhouse_api import (
    GreenhouseJobSchema,
    _parse_greenhouse_url,
    submit_greenhouse_application,
)
from pipelines.job_agent.models import (
    CandidateApplicationProfile,
    JobListing,
    load_application_profile,
)
from pipelines.job_agent.models.application import _load_cached

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "greenhouse_job_schema.json"
_PROFILE_PATH = (
    Path(__file__).resolve().parents[1] / "context" / "candidate_application.example.yaml"
)


@pytest.fixture(autouse=True)
def _clear_profile_cache() -> None:
    _load_cached.cache_clear()


@pytest.fixture
def profile() -> CandidateApplicationProfile:
    return load_application_profile(_PROFILE_PATH)


@pytest.fixture
def resume_file(tmp_path: Path) -> Path:
    path = tmp_path / "resume.pdf"
    path.write_bytes(b"%PDF-1.4\nfake resume bytes\n")
    return path


@pytest.fixture
def cover_letter_file(tmp_path: Path) -> Path:
    path = tmp_path / "cover_letter.pdf"
    path.write_bytes(b"%PDF-1.4\nfake cover letter bytes\n")
    return path


@pytest.fixture
def listing() -> JobListing:
    return JobListing(
        title="Senior Software Engineer",
        company="Example Corp",
        url="https://boards.greenhouse.io/examplecorp/jobs/4567890",
        dedup_key="examplecorp-senior-swe-4567890",
    )


class _StubFetcher:
    """Minimal stub satisfying the submitter's fetcher protocol."""

    def __init__(self, payload: Any) -> None:
        self._payload = payload
        self.calls: list[str] = []

    async def fetch_json(self, url: str) -> Any:
        self.calls.append(url)
        return self._payload


def _fixture_payload() -> dict[str, Any]:
    with _FIXTURE_PATH.open("r", encoding="utf-8") as f:
        data: Any = json.load(f)
    assert isinstance(data, dict)
    return data


def _deterministic_only_payload() -> dict[str, Any]:
    """Fixture trimmed to questions the deterministic mapper can handle."""
    full = _fixture_payload()
    keep_labels = {
        "First Name",
        "Last Name",
        "Email",
        "Phone",
        "Resume/CV",
        "Cover Letter",
        "Are you legally authorized to work in the United States?",
    }
    full["questions"] = [q for q in full["questions"] if q["label"] in keep_labels]
    return full


# ---------- URL parser ----------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://boards.greenhouse.io/examplecorp/jobs/4567890",
            ("examplecorp", "4567890"),
        ),
        (
            "https://job-boards.greenhouse.io/examplecorp/jobs/4567890",
            ("examplecorp", "4567890"),
        ),
        (
            "https://boards.greenhouse.io/embed/job_app?for=examplecorp&token=4567890",
            ("examplecorp", "4567890"),
        ),
        (
            "https://boards.greenhouse.io/examplecorp/jobs/4567890?gh_src=abc",
            ("examplecorp", "4567890"),
        ),
    ],
)
def test_parse_greenhouse_url_happy_path(url: str, expected: tuple[str, str]) -> None:
    assert _parse_greenhouse_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://jobs.lever.co/examplecorp/abcdef",
        "https://www.example.com/careers/12345",
        "not a url at all",
        "",
    ],
)
def test_parse_greenhouse_url_rejects_non_greenhouse(url: str) -> None:
    with pytest.raises(ValueError, match="Greenhouse"):
        _parse_greenhouse_url(url)


# ---------- Question-set parsing ----------


def test_parse_fixture_covers_every_field_type() -> None:
    schema = GreenhouseJobSchema.model_validate(_fixture_payload())
    field_types = {q.fields[0].type for q in schema.questions if q.fields}
    assert field_types == {
        "input_text",
        "input_file",
        "multi_value_single_select",
        "textarea",
        "multi_value_multi_select",
    }


def test_parse_fixture_preserves_select_options() -> None:
    schema = GreenhouseJobSchema.model_validate(_fixture_payload())
    single_select = next(
        q for q in schema.questions if q.fields[0].type == "multi_value_single_select"
    )
    labels = [opt.label for opt in single_select.fields[0].values]
    values = [opt.value for opt in single_select.fields[0].values]
    assert labels == ["Yes", "No"]
    assert values == ["1", "0"]


# ---------- End-to-end dry run ----------


@pytest.mark.asyncio
async def test_dry_run_maps_every_deterministic_field(
    profile: CandidateApplicationProfile,
    listing: JobListing,
    resume_file: Path,
    cover_letter_file: Path,
) -> None:
    fetcher = _StubFetcher(_deterministic_only_payload())

    attempt = await submit_greenhouse_application(
        listing,
        profile,
        resume_file,
        cover_letter_file,
        fetcher=fetcher,
        dry_run=True,
    )

    assert attempt.status == "awaiting_review"
    assert attempt.strategy == "api_greenhouse"
    assert attempt.dedup_key == listing.dedup_key
    # One GET to the expected endpoint, zero POSTs (dry run).
    assert fetcher.calls == [
        "https://boards-api.greenhouse.io/v1/boards/examplecorp/jobs/4567890?questions=true"
    ]

    payload = json.loads(attempt.summary)
    assert payload["first_name"] == profile.personal.first_name
    assert payload["last_name"] == profile.personal.last_name
    assert payload["email"] == profile.personal.email
    assert payload["phone"] == profile.personal.phone_formatted
    assert payload["resume"] == str(resume_file)
    assert payload["cover_letter"] == str(cover_letter_file)
    # Work authorization single-select resolved to the option's submittable value.
    auth_key = "question_4567890_9001"
    assert payload[auth_key] == "1"
    # fields_filled counts every mapped question (files are skipped).
    # First name, last name, email, phone, + the work-auth single-select.
    assert attempt.fields_filled == 5
    assert attempt.llm_calls_made == 0


@pytest.mark.asyncio
async def test_custom_textarea_raises_not_implemented(
    profile: CandidateApplicationProfile,
    listing: JobListing,
    resume_file: Path,
) -> None:
    fetcher = _StubFetcher(_fixture_payload())

    with pytest.raises(NotImplementedError, match="Prompt 07"):
        await submit_greenhouse_application(
            listing,
            profile,
            resume_file,
            None,
            fetcher=fetcher,
            dry_run=True,
        )


# ---------- Real POST error handling ----------


@pytest.mark.asyncio
async def test_422_validation_error_returns_error_attempt(
    profile: CandidateApplicationProfile,
    listing: JobListing,
    resume_file: Path,
) -> None:
    fetcher = _StubFetcher(_deterministic_only_payload())
    error_body = {
        "errors": [
            {"field": "email", "message": "is invalid"},
            {"field": "phone", "message": "is required"},
        ]
    }

    with respx.mock(base_url="https://boards-api.greenhouse.io") as mock:
        mock.post("/v1/boards/examplecorp/jobs/4567890").mock(
            return_value=httpx.Response(422, json=error_body)
        )
        attempt = await submit_greenhouse_application(
            listing,
            profile,
            resume_file,
            None,
            fetcher=fetcher,
            dry_run=False,
        )

    assert attempt.status == "error"
    assert attempt.strategy == "api_greenhouse"
    assert "422" in attempt.summary
    assert any("email" in e for e in attempt.errors)
    assert any("phone" in e for e in attempt.errors)
    # Every deterministic question (including the auth select) was filled
    # before the POST returned 422.
    assert attempt.fields_filled == 5
