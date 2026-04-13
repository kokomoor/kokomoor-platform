"""Tests for the Greenhouse Job Board API submitter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from pipelines.job_agent.application.submitters.greenhouse_api import (
    submit_greenhouse_application,
)
from pipelines.job_agent.application.qa_answerer import FormFieldAnswer
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


# ---------- End-to-end dry run ----------


@pytest.mark.asyncio
async def test_dry_run_maps_every_deterministic_field(
    profile: CandidateApplicationProfile,
    listing: JobListing,
    resume_file: Path,
    cover_letter_file: Path,
) -> None:
    payload = _deterministic_only_payload()
    
    with respx.mock(base_url="https://boards-api.greenhouse.io") as mock:
        mock.get("/v1/boards/examplecorp/jobs/4567890?questions=true").mock(
            return_value=httpx.Response(200, json=payload)
        )
        
        async with httpx.AsyncClient() as client:
            attempt = await submit_greenhouse_application(
                listing,
                profile,
                resume_file,
                cover_letter_file,
                client=client,
                dry_run=True,
            )

    assert attempt.status == "awaiting_review"
    assert attempt.strategy == "api_greenhouse"
    assert attempt.dedup_key == listing.dedup_key

    parsed_payload = json.loads(attempt.summary)
    assert parsed_payload["first_name"] == profile.personal.first_name
    assert parsed_payload["last_name"] == profile.personal.last_name
    assert parsed_payload["email"] == profile.personal.email
    assert parsed_payload["phone"] == profile.personal.phone_formatted
    assert parsed_payload["resume"] == str(resume_file)
    assert parsed_payload["cover_letter"] == str(cover_letter_file)
    
    auth_key = "question_4567890_9001"
    assert parsed_payload[auth_key] == "1"
    assert attempt.fields_filled == 5
    assert attempt.llm_calls_made == 0


@pytest.mark.asyncio
async def test_custom_textarea_calls_llm_fallback(
    profile: CandidateApplicationProfile,
    listing: JobListing,
    resume_file: Path,
) -> None:
    payload = _fixture_payload()
    
    llm = MagicMock()
    mock_answer = FormFieldAnswer(answer="LLM answer", confidence=0.9, source="profile")
    
    with respx.mock(base_url="https://boards-api.greenhouse.io") as mock:
        mock.get("/v1/boards/examplecorp/jobs/4567890?questions=true").mock(
            return_value=httpx.Response(200, json=payload)
        )
        
        with pytest.MonkeyPatch().context() as mp:
            async def side_effect(*args, **kwargs):
                return mock_answer
            mp.setattr("pipelines.job_agent.application.submitters.greenhouse_api.answer_application_question", side_effect)
            
            async with httpx.AsyncClient() as client:
                attempt = await submit_greenhouse_application(
                    listing,
                    profile,
                    resume_file,
                    None,
                    llm=llm,
                    client=client,
                    dry_run=True,
                )

    assert attempt.status == "awaiting_review"
    assert attempt.llm_calls_made > 0
    parsed_payload = json.loads(attempt.summary)
    # Check for a known custom field name from fixture
    assert "question_4567890_9002" in parsed_payload # LinkedIn URL (but maybe mapped)
    # The actual custom question in fixture is likely something like 'Why work here'
    # Greenhouse IDs look like question_<jobid>_<qid>
    found_llm_answer = False
    for k, v in parsed_payload.items():
        if v == "LLM answer":
            found_llm_answer = True
            break
    assert found_llm_answer


# ---------- Real POST error handling ----------


@pytest.mark.asyncio
async def test_422_validation_error_returns_error_attempt(
    profile: CandidateApplicationProfile,
    listing: JobListing,
    resume_file: Path,
) -> None:
    payload = _deterministic_only_payload()
    error_body = {
        "errors": [
            {"field": "email", "message": "is invalid"},
            {"field": "phone", "message": "is required"},
        ]
    }

    with respx.mock(base_url="https://boards-api.greenhouse.io") as mock:
        mock.get("/v1/boards/examplecorp/jobs/4567890?questions=true").mock(
            return_value=httpx.Response(200, json=payload)
        )
        mock.post("/v1/boards/examplecorp/jobs/4567890").mock(
            return_value=httpx.Response(422, json=error_body)
        )
        
        async with httpx.AsyncClient() as client:
            attempt = await submit_greenhouse_application(
                listing,
                profile,
                resume_file,
                None,
                client=client,
                dry_run=False,
            )

    assert attempt.status == "error"
    assert attempt.strategy == "api_greenhouse"
    assert any("email" in e for e in attempt.errors)
    assert any("phone" in e for e in attempt.errors)
    assert attempt.fields_filled == 5
