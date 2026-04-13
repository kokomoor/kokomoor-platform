"""Greenhouse Job Board API submitter.

Submits job applications via the public Greenhouse Job Board API
without opening a browser. Uses the deterministic field mapper for
standard fields and the LLM QA answerer for custom questions.
"""

from __future__ import annotations

import json
import mimetypes
import re
from typing import TYPE_CHECKING, Any

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, Field

from playwright.async_api import Page
from pipelines.job_agent.application.field_mapper import map_field
from pipelines.job_agent.application.qa_answerer import answer_application_question
from pipelines.job_agent.application.registry import register_submitter
from pipelines.job_agent.application.router import SubmissionStrategy
from pipelines.job_agent.application.submitters._common import post_with_backoff
from pipelines.job_agent.models import ApplicationAttempt

if TYPE_CHECKING:
    from pathlib import Path

    from core.llm.protocol import LLMClient
    from pipelines.job_agent.models import (
        CandidateApplicationProfile,
        JobListing,
    )

logger = structlog.get_logger(__name__)

_API_BASE = "https://boards-api.greenhouse.io/v1/boards"
_STRATEGY = SubmissionStrategy.API_GREENHOUSE

_TEXT_TYPES = frozenset({"input_text", "input_hidden", "textarea"})
_SELECT_TYPES = frozenset({"multi_value_single_select", "multi_value_multi_select"})
_FILE_TYPES = frozenset({"input_file"})


# ---------- URL parser ----------


_URL_RE = re.compile(
    r"(?:boards|job-boards)\.greenhouse\.io/"
    r"(?:embed/job_app\?for=(?P<eslug>[^&]+)&token=(?P<eid>\d+)"
    r"|(?P<slug>[^/]+)/jobs/(?P<id>\d+))"
)


def _parse_greenhouse_url(url: str) -> tuple[str, str]:
    """Extract ``(board_slug, job_id)`` from a Greenhouse job URL."""
    match = _URL_RE.search(url)
    if match is None:
        msg = f"URL does not look like a Greenhouse job listing: {url!r}"
        raise ValueError(msg)
    slug = match.group("slug") or match.group("eslug")
    job_id = match.group("id") or match.group("eid")
    if not slug or not job_id:
        msg = f"Could not extract slug and job id from Greenhouse URL: {url!r}"
        raise ValueError(msg)
    return slug, job_id


# ---------- Schema models (internal) ----------


class _GreenhouseOption(BaseModel):
    model_config = ConfigDict(extra="allow")
    label: str
    value: str = ""


class _GreenhouseField(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    type: str
    values: list[_GreenhouseOption] = Field(default_factory=list)


class _GreenhouseQuestion(BaseModel):
    model_config = ConfigDict(extra="allow")
    label: str = ""
    required: bool = False
    fields: list[_GreenhouseField] = Field(default_factory=list)


class GreenhouseJobSchema(BaseModel):
    model_config = ConfigDict(extra="allow")
    questions: list[_GreenhouseQuestion] = Field(default_factory=list)


# ---------- Logic ----------


async def _fetch_question_set(
    client: httpx.AsyncClient, slug: str, job_id: str
) -> GreenhouseJobSchema:
    """GET the job's question set from the public Job Board API."""
    url = f"{_API_BASE}/{slug}/jobs/{job_id}?questions=true"
    resp = await client.get(url)
    resp.raise_for_status()
    return GreenhouseJobSchema.model_validate(resp.json())


def _options_for(field: _GreenhouseField) -> list[str] | None:
    if field.type in _SELECT_TYPES and field.values:
        return [opt.label for opt in field.values]
    return None


def _submit_value_for_option(field: _GreenhouseField, label: str) -> str:
    for opt in field.values:
        if opt.label == label:
            return opt.value or label
    return label


async def _map_questions(
    schema: GreenhouseJobSchema,
    profile: CandidateApplicationProfile,
    llm: LLMClient | None,
    listing: JobListing,
    run_id: str,
) -> tuple[dict[str, str], int, int]:
    answers: dict[str, str] = {}
    fields_filled = 0
    llm_calls_made = 0

    profile_text = ""  # We might want to pass the profile as text for the LLM
    # For now, we'll use a simple representation or just the object if the answerer allows.
    # The answerer expects 'candidate_profile: str'.
    profile_text = profile.model_dump_json() # Or a better YAML-like string

    for question in schema.questions:
        if not question.fields:
            continue
        field = question.fields[0]
        if field.type in _FILE_TYPES:
            continue

        options = _options_for(field)
        mapping = map_field(question.label, field.type, options, profile)

        if mapping.confidence >= 0.8:
            value = mapping.value
            if field.type in _SELECT_TYPES:
                value = _submit_value_for_option(field, value)
            answers[field.name] = value
            fields_filled += 1
            continue

        # LLM fallback
        if llm:
            logger.info("greenhouse_llm_fallback", question=question.label, field=field.name)
            qa_result = await answer_application_question(
                llm=llm,
                field_label=question.label,
                field_type=field.type,
                field_options=options,
                candidate_profile=profile_text,
                job_title=listing.title,
                company=listing.company,
                run_id=run_id,
            )
            value = qa_result.answer
            if field.type in _SELECT_TYPES:
                value = _submit_value_for_option(field, value)
            answers[field.name] = value
            fields_filled += 1
            llm_calls_made += 1
        else:
            logger.warning("greenhouse_no_llm_for_custom_question", question=question.label)

    return answers, fields_filled, llm_calls_made


def _build_payload(
    profile: CandidateApplicationProfile,
    answers: dict[str, str],
    resume_path: Path,
    cover_letter_path: Path | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "first_name": profile.personal.first_name,
        "last_name": profile.personal.last_name,
        "email": profile.personal.email,
        "phone": profile.personal.phone_formatted,
    }
    if profile.personal.linkedin_url:
        payload["mapped_url_linkedin"] = profile.personal.linkedin_url
    if profile.personal.github_url:
        payload["mapped_url_github"] = profile.personal.github_url
    if profile.personal.portfolio_url:
        payload["mapped_url_portfolio"] = profile.personal.portfolio_url
    payload["resume"] = str(resume_path)
    if cover_letter_path is not None:
        payload["cover_letter"] = str(cover_letter_path)
    payload.update(answers)
    return payload


# ---------- Public entrypoint ----------


async def submit_greenhouse_application(
    listing: JobListing,
    profile: CandidateApplicationProfile,
    resume_path: Path,
    cover_letter_path: Path | None,
    *,
    client: httpx.AsyncClient | None = None,
    page: Page | None = None,
    llm: LLMClient | None = None,
    run_id: str = "",
    dry_run: bool = True,
) -> ApplicationAttempt:
    """Submit a Greenhouse application via the public Job Board API."""
    if client is None:
        raise ValueError("httpx.AsyncClient is required for Greenhouse API submission.")

    slug, job_id = _parse_greenhouse_url(listing.url)
    schema = await _fetch_question_set(client, slug, job_id)
    answers, fields_filled, llm_calls_made = await _map_questions(
        schema, profile, llm, listing, run_id
    )
    payload = _build_payload(profile, answers, resume_path, cover_letter_path)

    if dry_run:
        return ApplicationAttempt(
            dedup_key=listing.dedup_key,
            status="awaiting_review",
            strategy=_STRATEGY.value,
            summary=json.dumps(payload, sort_keys=True, default=str),
            steps_taken=fields_filled,
            fields_filled=fields_filled,
            llm_calls_made=llm_calls_made,
        )

    url = f"{_API_BASE}/{slug}/jobs/{job_id}"
    data = {k: v for k, v in payload.items() if k not in {"resume", "cover_letter"}}

    resume_mime = mimetypes.guess_type(resume_path.name)[0] or "application/octet-stream"
    files = {
        "resume": (resume_path.name, resume_path.read_bytes(), resume_mime),
    }
    if cover_letter_path:
        cl_mime = mimetypes.guess_type(cover_letter_path.name)[0] or "application/octet-stream"
        files["cover_letter"] = (cover_letter_path.name, cover_letter_path.read_bytes(), cl_mime)

    try:
        resp = await post_with_backoff(client, url, data=data, files=files, source="greenhouse")
    except httpx.HTTPError as exc:
        return ApplicationAttempt(
            dedup_key=listing.dedup_key,
            status="error",
            strategy=_STRATEGY.value,
            summary=f"HTTP error: {exc}",
            errors=[str(exc)],
            fields_filled=fields_filled,
            llm_calls_made=llm_calls_made,
        )

    if 200 <= resp.status_code < 300:
        return ApplicationAttempt(
            dedup_key=listing.dedup_key,
            status="submitted",
            strategy=_STRATEGY.value,
            summary=f"Submitted via Greenhouse API ({resp.status_code}).",
            fields_filled=fields_filled,
            llm_calls_made=llm_calls_made,
        )

    # Error handling...
    return ApplicationAttempt(
        dedup_key=listing.dedup_key,
        status="error",
        strategy=_STRATEGY.value,
        summary=f"Unexpected response ({resp.status_code}).",
        errors=[resp.text[:500]],
        fields_filled=fields_filled,
        llm_calls_made=llm_calls_made,
    )


# Register the submitter
register_submitter(_STRATEGY, submit_greenhouse_application)
