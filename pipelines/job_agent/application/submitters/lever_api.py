"""Lever Postings API submitter.

Submits job applications via the public Lever Postings API
without opening a browser.
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

_API_BASE = "https://api.lever.co/v0/postings"
_STRATEGY = SubmissionStrategy.API_LEVER


# ---------- URL parser ----------


_URL_RE = re.compile(r"jobs\.lever\.co/(?P<slug>[^/]+)/(?P<id>[a-f0-9-]+)")


def _parse_lever_url(url: str) -> tuple[str, str]:
    """Extract ``(company_slug, posting_id)`` from a Lever job URL."""
    match = _URL_RE.search(url)
    if match is None:
        msg = f"URL does not look like a Lever job listing: {url!r}"
        raise ValueError(msg)
    slug = match.group("slug")
    posting_id = match.group("id")
    return slug, posting_id


# ---------- Schema models (internal) ----------


class _LeverQuestion(BaseModel):
    model_config = ConfigDict(extra="allow")
    text: str
    required: bool = False


class LeverPostingSchema(BaseModel):
    model_config = ConfigDict(extra="allow")
    questions: list[_LeverQuestion] = Field(default_factory=list)


# ---------- Logic ----------


async def _fetch_posting_schema(
    client: httpx.AsyncClient, slug: str, posting_id: str
) -> LeverPostingSchema:
    """GET the job's details from the public Postings API."""
    url = f"{_API_BASE}/{slug}/{posting_id}"
    resp = await client.get(url)
    resp.raise_for_status()
    return LeverPostingSchema.model_validate(resp.json())


async def _map_questions(
    schema: LeverPostingSchema,
    profile: CandidateApplicationProfile,
    llm: LLMClient | None,
    listing: JobListing,
    run_id: str,
) -> tuple[list[dict[str, Any]], int, int]:
    cards: list[dict[str, Any]] = []
    fields_filled = 0
    llm_calls_made = 0

    profile_text = profile.model_dump_json()

    for question in schema.questions:
        mapping = map_field(question.text, "text", None, profile)
        if mapping.confidence >= 0.8:
            cards.append({"text": question.text, "value": mapping.value})
            fields_filled += 1
            continue

        # LLM fallback
        if llm:
            logger.info("lever_llm_fallback", question=question.text)
            qa_result = await answer_application_question(
                llm=llm,
                field_label=question.text,
                field_type="text",
                candidate_profile=profile_text,
                job_title=listing.title,
                company=listing.company,
                run_id=run_id,
            )
            cards.append({"text": question.text, "value": qa_result.answer})
            fields_filled += 1
            llm_calls_made += 1
        else:
            logger.warning("lever_no_llm_for_custom_question", question=question.text)

    return cards, fields_filled, llm_calls_made


def _build_payload(
    profile: CandidateApplicationProfile,
    cards: list[dict[str, Any]],
    resume_path: Path,
    cover_letter_text: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": f"{profile.personal.first_name} {profile.personal.last_name}",
        "email": profile.personal.email,
        "phone": profile.personal.phone_formatted,
        "org": profile.screening.how_did_you_hear or "Online",
    }

    if profile.personal.linkedin_url:
        payload["urls[LinkedIn]"] = profile.personal.linkedin_url
    if profile.personal.github_url:
        payload["urls[GitHub]"] = profile.personal.github_url
    if profile.personal.portfolio_url:
        payload["urls[Portfolio]"] = profile.personal.portfolio_url

    if cover_letter_text:
        payload["comments"] = cover_letter_text

    payload["resume"] = str(resume_path)

    for i, card in enumerate(cards):
        payload[f"cards[{i}][text]"] = card["text"]
        payload[f"cards[{i}][value]"] = card["value"]

    return payload


# ---------- Public entrypoint ----------


async def submit_lever_application(
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
    """Submit a Lever application via the public Postings API."""
    if client is None:
        raise ValueError("httpx.AsyncClient is required for Lever API submission.")

    slug, posting_id = _parse_lever_url(listing.url)

    try:
        schema = await _fetch_posting_schema(client, slug, posting_id)
    except Exception as exc:
        logger.warning("lever_schema_fetch_failed", error=str(exc), url=listing.url)
        schema = LeverPostingSchema(questions=[])

    cover_letter_text = None
    if cover_letter_path:
        cover_letter_text = "See attached cover letter."

    cards, fields_filled, llm_calls_made = await _map_questions(
        schema, profile, llm, listing, run_id
    )
    payload = _build_payload(profile, cards, resume_path, cover_letter_text)

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

    url = f"{_API_BASE}/{slug}/{posting_id}"
    data = {k: v for k, v in payload.items() if k != "resume"}

    resume_mime = mimetypes.guess_type(resume_path.name)[0] or "application/octet-stream"
    files = {
        "resume": (resume_path.name, resume_path.read_bytes(), resume_mime),
    }

    try:
        resp = await post_with_backoff(client, url, data=data, files=files, source="lever")
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
            summary=f"Submitted via Lever API ({resp.status_code}).",
            fields_filled=fields_filled,
            llm_calls_made=llm_calls_made,
        )

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
register_submitter(_STRATEGY, submit_lever_application)
