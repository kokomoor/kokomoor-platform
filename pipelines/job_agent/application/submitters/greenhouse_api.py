"""Greenhouse Job Board API submitter.

Submits job applications via the public Greenhouse Job Board API
without opening a browser. Two endpoints, both unauthenticated for
discovery:

- ``GET  https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{id}?questions=true``
- ``POST https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{id}``

The submitter uses the deterministic field mapper for every question
it recognizes (``confidence >= 0.8``) and, starting in Prompt 07, the
LLM QA answerer for the rest. Today, hitting a custom question raises
:class:`NotImplementedError` so the wiring is explicit but the escape
hatch is loud.

Dry-run mode (``dry_run=True``, the default) skips the POST entirely
and returns an :class:`ApplicationAttempt` with
``status="awaiting_review"`` and a ``summary`` containing the JSON
payload that would have been sent. This keeps CI and the first live
runs hermetic and easy to review.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, Field

from core.fetch.http_client import HttpFetcher
from pipelines.job_agent.application.field_mapper import map_field
from pipelines.job_agent.application.models import ApplicationAttempt

if TYPE_CHECKING:
    from pathlib import Path

    from core.llm.protocol import LLMClient
    from pipelines.job_agent.models import (
        CandidateApplicationProfile,
        JobListing,
    )

logger = structlog.get_logger(__name__)

_API_BASE = "https://boards-api.greenhouse.io/v1/boards"
_STRATEGY = "api_greenhouse"

_TEXT_TYPES = frozenset({"input_text", "input_hidden", "textarea"})
_SELECT_TYPES = frozenset({"multi_value_single_select", "multi_value_multi_select"})
_FILE_TYPES = frozenset({"input_file"})

_MAX_429_RETRIES = 3
_DEFAULT_429_BACKOFF_SECONDS = 5.0


# ---------- URL parser ----------


# Accepts both the public board form and the embedded-iframe form:
#   https://boards.greenhouse.io/<slug>/jobs/<id>
#   https://job-boards.greenhouse.io/<slug>/jobs/<id>
#   https://boards.greenhouse.io/embed/job_app?for=<slug>&token=<id>
_URL_RE = re.compile(
    r"(?:boards|job-boards)\.greenhouse\.io/"
    r"(?:embed/job_app\?for=(?P<eslug>[^&]+)&token=(?P<eid>\d+)"
    r"|(?P<slug>[^/]+)/jobs/(?P<id>\d+))"
)


def _parse_greenhouse_url(url: str) -> tuple[str, str]:
    """Extract ``(board_slug, job_id)`` from a Greenhouse job URL.

    Raises:
        ValueError: If the URL is not a recognized Greenhouse job URL.
    """
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
    """One field inside a Greenhouse question.

    Most questions carry exactly one field. ``name`` is the key used in
    the submission multipart payload and ``type`` dictates how to fill
    and reconcile it.
    """

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
    """Parsed ``?questions=true`` payload for a Greenhouse job."""

    model_config = ConfigDict(extra="allow")

    questions: list[_GreenhouseQuestion] = Field(default_factory=list)


# ---------- Fetcher protocol ----------


@runtime_checkable
class _SupportsFetchJson(Protocol):
    """Minimal structural contract for the ``GET`` side of the submitter.

    ``HttpFetcher`` satisfies this natively; tests pass a tiny stub that
    returns a captured fixture so no real network I/O happens.
    """

    async def fetch_json(self, url: str) -> Any: ...


async def _fetch_question_set(
    fetcher: _SupportsFetchJson, slug: str, job_id: str
) -> GreenhouseJobSchema:
    """GET the job's question set from the public Job Board API."""
    url = f"{_API_BASE}/{slug}/jobs/{job_id}?questions=true"
    raw: Any = await fetcher.fetch_json(url)
    return GreenhouseJobSchema.model_validate(raw)


# ---------- Field reconciliation ----------


def _options_for(field: _GreenhouseField) -> list[str] | None:
    """Return option labels for a select/multi-select field, else None."""
    if field.type in _SELECT_TYPES and field.values:
        return [opt.label for opt in field.values]
    return None


def _submit_value_for_option(field: _GreenhouseField, label: str) -> str:
    """Translate a chosen option label back to its submittable value."""
    for opt in field.values:
        if opt.label == label:
            return opt.value or label
    return label


def _build_payload(
    profile: CandidateApplicationProfile,
    answers: dict[str, str],
    resume_path: Path,
    cover_letter_path: Path | None,
) -> dict[str, Any]:
    """Assemble the full submission payload as a serializable dict.

    This is the dict that the real POST turns into multipart form data
    and that dry-run serializes into the attempt's ``summary``.
    """
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


def _map_questions(
    schema: GreenhouseJobSchema,
    profile: CandidateApplicationProfile,
    llm: LLMClient | None,
) -> tuple[dict[str, str], int, int]:
    """Walk every question and map it to a profile value.

    Returns ``(answers_by_field_name, fields_filled, llm_calls_made)``.
    Raises :class:`NotImplementedError` on any question that needs the
    LLM QA answerer until Prompt 07 wires it in.
    """
    answers: dict[str, str] = {}
    fields_filled = 0
    llm_calls_made = 0

    for question in schema.questions:
        if not question.fields:
            continue
        field = question.fields[0]
        if field.type in _FILE_TYPES:
            # Resume / cover letter come from the payload builder,
            # not from the field mapper.
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
        # Reserved interface: the LLM path is wired in Prompt 07.
        _ = llm
        msg = (
            f"Greenhouse question {field.name!r} (label={question.label!r}) "
            f"needs the LLM QA answerer, which is wired in Prompt 07."
        )
        raise NotImplementedError(msg)

    return answers, fields_filled, llm_calls_made


# ---------- POST helpers ----------


def _parse_retry_after(raw: str) -> float:
    """Parse a ``Retry-After`` header value in seconds."""
    try:
        return max(float(raw), 0.0)
    except ValueError:
        return _DEFAULT_429_BACKOFF_SECONDS


def _extract_422_errors(body: Any) -> list[str]:
    """Flatten a 422 response body into a list of ``field: message`` strings."""
    errors: list[str] = []
    if isinstance(body, dict):
        raw = body.get("errors") or body.get("error_messages") or body.get("error")
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    field_name = str(item.get("field", ""))
                    message = str(item.get("message", "")) or json.dumps(
                        item, sort_keys=True, default=str
                    )
                    errors.append(f"{field_name}: {message}" if field_name else message)
                else:
                    errors.append(str(item))
        elif isinstance(raw, dict):
            errors.extend(f"{k}: {v}" for k, v in raw.items())
        elif isinstance(raw, str):
            errors.append(raw)
    if not errors:
        errors.append(json.dumps(body, sort_keys=True, default=str))
    return errors


def _read_files(
    resume_path: Path, cover_letter_path: Path | None
) -> dict[str, tuple[str, bytes, str]]:
    """Read resume / cover letter into the httpx multipart files shape."""
    files: dict[str, tuple[str, bytes, str]] = {
        "resume": (
            resume_path.name,
            resume_path.read_bytes(),
            "application/pdf",
        ),
    }
    if cover_letter_path is not None:
        files["cover_letter"] = (
            cover_letter_path.name,
            cover_letter_path.read_bytes(),
            "application/pdf",
        )
    return files


async def _post_with_backoff(
    client: httpx.AsyncClient,
    url: str,
    *,
    data: dict[str, str],
    files: dict[str, tuple[str, bytes, str]],
) -> httpx.Response:
    """POST with a bounded retry loop that honors ``Retry-After`` on 429."""
    resp = await client.post(url, data=data, files=files)
    for attempt in range(_MAX_429_RETRIES):
        if resp.status_code != 429:
            return resp
        wait = _parse_retry_after(resp.headers.get("Retry-After", ""))
        logger.warning(
            "greenhouse_429_retry",
            attempt=attempt + 1,
            retry_after=wait,
            url=url,
        )
        await asyncio.sleep(wait)
        resp = await client.post(url, data=data, files=files)
    return resp


# ---------- Public entrypoint ----------


async def submit_greenhouse_application(
    listing: JobListing,
    profile: CandidateApplicationProfile,
    resume_path: Path,
    cover_letter_path: Path | None,
    *,
    llm: LLMClient | None = None,
    run_id: str = "",
    dry_run: bool = True,
    fetcher: _SupportsFetchJson | None = None,
) -> ApplicationAttempt:
    """Submit a Greenhouse application via the public Job Board API.

    Args:
        listing: The :class:`JobListing` being applied to. Its ``url`` is
            parsed for the Greenhouse slug and job id; its ``dedup_key``
            is copied onto the returned :class:`ApplicationAttempt`.
        profile: The loaded candidate application profile.
        resume_path: Path to the resume PDF to upload.
        cover_letter_path: Path to the cover letter PDF, or ``None``.
        llm: LLM client for the QA answerer path. Unused in this prompt
            — Prompt 07 wires it up.
        run_id: Pipeline run identifier for log correlation.
        dry_run: If ``True`` (default), skip the POST and return an
            ``awaiting_review`` attempt with the payload in ``summary``.
        fetcher: Optional fetcher override so tests can return a fixture
            instead of hitting the real API. Defaults to
            :class:`HttpFetcher`.

    Returns:
        An :class:`ApplicationAttempt` describing the outcome.
    """
    slug, job_id = _parse_greenhouse_url(listing.url)
    schema = await _fetch_question_set(fetcher or HttpFetcher(), slug, job_id)
    answers, fields_filled, llm_calls_made = _map_questions(schema, profile, llm)
    payload = _build_payload(profile, answers, resume_path, cover_letter_path)

    if dry_run:
        logger.info(
            "greenhouse_dry_run",
            run_id=run_id,
            slug=slug,
            job_id=job_id,
            fields_filled=fields_filled,
        )
        return ApplicationAttempt(
            dedup_key=listing.dedup_key,
            status="awaiting_review",
            strategy=_STRATEGY,
            summary=json.dumps(payload, sort_keys=True, default=str),
            steps_taken=fields_filled,
            fields_filled=fields_filled,
            llm_calls_made=llm_calls_made,
        )

    url = f"{_API_BASE}/{slug}/jobs/{job_id}"
    data = {k: v for k, v in payload.items() if k not in {"resume", "cover_letter"}}
    files = _read_files(resume_path, cover_letter_path)

    try:
        async with httpx.AsyncClient() as client:
            resp = await _post_with_backoff(client, url, data=data, files=files)
    except httpx.HTTPError as exc:
        return ApplicationAttempt(
            dedup_key=listing.dedup_key,
            status="error",
            strategy=_STRATEGY,
            summary=f"HTTP error posting to Greenhouse: {exc}",
            errors=[str(exc)],
            fields_filled=fields_filled,
            llm_calls_made=llm_calls_made,
        )

    if 200 <= resp.status_code < 300:
        return ApplicationAttempt(
            dedup_key=listing.dedup_key,
            status="submitted",
            strategy=_STRATEGY,
            summary=f"Submitted via Greenhouse Job Board API ({resp.status_code}).",
            fields_filled=fields_filled,
            llm_calls_made=llm_calls_made,
        )
    if resp.status_code == 422:
        try:
            body: Any = resp.json()
        except ValueError:
            body = {"raw": resp.text}
        errors = _extract_422_errors(body)
        return ApplicationAttempt(
            dedup_key=listing.dedup_key,
            status="error",
            strategy=_STRATEGY,
            summary=f"Greenhouse 422 validation error ({len(errors)} field(s)).",
            errors=errors,
            fields_filled=fields_filled,
            llm_calls_made=llm_calls_made,
        )
    if resp.status_code == 429:
        retry_after = resp.headers.get("Retry-After", "")
        return ApplicationAttempt(
            dedup_key=listing.dedup_key,
            status="stuck",
            strategy=_STRATEGY,
            summary=f"Greenhouse rate-limited (429); Retry-After={retry_after!r}.",
            errors=[f"429 Retry-After={retry_after}"],
            fields_filled=fields_filled,
            llm_calls_made=llm_calls_made,
        )
    return ApplicationAttempt(
        dedup_key=listing.dedup_key,
        status="error",
        strategy=_STRATEGY,
        summary=f"Unexpected Greenhouse response ({resp.status_code}).",
        errors=[f"HTTP {resp.status_code}: {resp.text[:500]}"],
        fields_filled=fields_filled,
        llm_calls_made=llm_calls_made,
    )
