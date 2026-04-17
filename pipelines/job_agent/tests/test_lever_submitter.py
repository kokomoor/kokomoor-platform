"""Tests for the Lever Postings API submitter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from pipelines.job_agent.application.submitters.lever_api import (
    _parse_lever_url,
    submit_lever_application,
)
from pipelines.job_agent.application.qa_answerer import FormFieldAnswer
from pipelines.job_agent.models import (
    CandidateApplicationProfile,
    JobListing,
    load_application_profile,
)
from pipelines.job_agent.models.application import _load_cached

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "lever_job_schema.json"
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
def listing() -> JobListing:
    return JobListing(
        title="Senior Software Engineer",
        company="Example Corp",
        url="https://jobs.lever.co/examplecorp/12345678-abcd-1234-abcd-1234567890ab",
        dedup_key="examplecorp-senior-swe-12345678",
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
        "Are you authorized to work in the United States?",
    }
    full["questions"] = [q for q in full["questions"] if q["text"] in keep_labels]
    return full


# ---------- URL parser ----------


def test_parse_lever_url() -> None:
    slug, posting_id = _parse_lever_url("https://jobs.lever.co/examplecorp/12345678-abcd-1234-abcd-1234567890ab")
    assert slug == "examplecorp"
    assert posting_id == "12345678-abcd-1234-abcd-1234567890ab"

    with pytest.raises(ValueError, match="not look like a Lever job listing"):
        _parse_lever_url("https://example.com/not-lever")


# ---------- End-to-end dry run ----------


@pytest.mark.asyncio
async def test_dry_run_maps_every_deterministic_field(
    profile: CandidateApplicationProfile,
    listing: JobListing,
    resume_file: Path,
) -> None:
    payload = _deterministic_only_payload()
    
    with respx.mock(base_url="https://api.lever.co") as mock:
        mock.get("/v0/postings/examplecorp/12345678-abcd-1234-abcd-1234567890ab").mock(
            return_value=httpx.Response(200, json=payload)
        )
        
        async with httpx.AsyncClient() as client:
            attempt = await submit_lever_application(
                listing,
                profile,
                resume_file,
                None,
                client=client,
                dry_run=True,
            )

    assert attempt.status == "awaiting_review"
    assert attempt.strategy == "api_lever"
    assert attempt.dedup_key == listing.dedup_key

    parsed_payload = json.loads(attempt.summary)
    assert parsed_payload["name"] == f"{profile.personal.first_name} {profile.personal.last_name}"
    assert parsed_payload["email"] == profile.personal.email
    assert parsed_payload["phone"] == profile.personal.phone_formatted
    assert parsed_payload["resume"] == str(resume_file)
    
    assert parsed_payload["urls[LinkedIn]"] == profile.personal.linkedin_url
    
    assert parsed_payload["cards[0][text]"] == "Are you authorized to work in the United States?"
    assert parsed_payload["cards[0][value]"] == "Yes"
    
    assert attempt.fields_filled == 1
    assert attempt.llm_calls_made == 0


@pytest.mark.asyncio
async def test_custom_question_calls_llm_fallback(
    profile: CandidateApplicationProfile,
    listing: JobListing,
    resume_file: Path,
) -> None:
    payload = _fixture_payload()
    
    # Mock LLM client
    llm = MagicMock()
    mock_answer = FormFieldAnswer(answer="I want to work here because...", confidence=0.9, source="profile")
    
    with respx.mock(base_url="https://api.lever.co") as mock:
        mock.get("/v0/postings/examplecorp/12345678-abcd-1234-abcd-1234567890ab").mock(
            return_value=httpx.Response(200, json=payload)
        )
        
        # We need to mock structured_complete or answer_application_question
        with MagicMock() as mock_aq:
            from pipelines.job_agent.application.submitters import lever_api
            # Patch the answer_application_question call in lever_api
            import pipelines.job_agent.application.submitters.lever_api as lever_api_mod
            
            async def side_effect(*args, **kwargs):
                return mock_answer
            
            # Use pytest-mock or patch
            with pytest.MonkeyPatch().context() as mp:
                mp.setattr("pipelines.job_agent.application.submitters.lever_api.answer_application_question", side_effect)
                
                async with httpx.AsyncClient() as client:
                    attempt = await submit_lever_application(
                        listing,
                        profile,
                        resume_file,
                        None,
                        llm=llm,
                        client=client,
                        dry_run=True,
                    )

    assert attempt.status == "awaiting_review"
    assert attempt.llm_calls_made == 1
    parsed_payload = json.loads(attempt.summary)
    # Check if the LLM answer is in the payload
    # Lever questions are in cards[i][text] and cards[i][value]
    found = False
    for i in range(10):
        key = f"cards[{i}][text]"
        if key in parsed_payload and "Why do you want" in parsed_payload[key]:
            assert parsed_payload[f"cards[{i}][value]"] == "I want to work here because..."
            found = True
            break
    assert found
