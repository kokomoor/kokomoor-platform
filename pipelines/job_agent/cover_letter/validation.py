"""Deterministic normalization and validation for cover letters."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pipelines.job_agent.cover_letter.models import CoverLetterDocument, CoverLetterPlan

if TYPE_CHECKING:
    from pipelines.job_agent.models.resume_tailoring import ResumeMasterProfile

_PLACEHOLDER_PATTERNS = (
    r"\[company\]",
    r"\[hiring manager\]",
    r"\{\{.+?\}\}",
    r"\bTBD\b",
    r"<[^>]+>",
)

_MAX_WORDS = 430
_MIN_WORDS = 220


@dataclass(frozen=True)
class CoverLetterValidationResult:
    """Validated and normalized cover-letter output."""

    plan: CoverLetterPlan
    document: CoverLetterDocument


def validate_cover_letter_plan(
    *,
    plan: CoverLetterPlan,
    profile: ResumeMasterProfile,
    expected_company: str,
) -> CoverLetterValidationResult:
    """Validate, normalize, and convert an LLM plan into renderable structure."""
    normalized_plan = plan.model_copy(
        update={
            "salutation": _normalize_salutation(plan.salutation),
            "opening_paragraph": _normalize_paragraph(plan.opening_paragraph),
            "body_paragraphs": [_normalize_paragraph(p) for p in plan.body_paragraphs],
            "closing_paragraph": _normalize_paragraph(plan.closing_paragraph),
            "signoff": _normalize_signoff(plan.signoff),
            "signature_name": _normalize_whitespace(plan.signature_name),
            "company_motivation": _normalize_paragraph(plan.company_motivation),
            "job_requirements_addressed": [
                _normalize_paragraph(x) for x in plan.job_requirements_addressed
            ],
            "selected_experience_ids": _dedupe_preserve(plan.selected_experience_ids),
            "selected_education_ids": _dedupe_preserve(plan.selected_education_ids),
            "selected_bullet_ids": _dedupe_preserve(plan.selected_bullet_ids),
            "tone_version": _normalize_whitespace(plan.tone_version),
        }
    )

    _ensure_id_references_exist(normalized_plan, profile)
    _ensure_no_placeholders(normalized_plan)
    _ensure_complete_sentences(normalized_plan)
    _ensure_no_duplicate_claims(normalized_plan)
    _ensure_company_reference_is_supported(normalized_plan, expected_company)
    _ensure_word_budget(normalized_plan)

    document = CoverLetterDocument(
        salutation=normalized_plan.salutation,
        opening_paragraph=normalized_plan.opening_paragraph,
        body_paragraphs=normalized_plan.body_paragraphs,
        closing_paragraph=normalized_plan.closing_paragraph,
        signoff=normalized_plan.signoff,
        signature_name=normalized_plan.signature_name,
    )
    return CoverLetterValidationResult(plan=normalized_plan, document=document)


def _normalize_paragraph(text: str) -> str:
    normalized = _normalize_whitespace(text)
    normalized = normalized.replace("\u2014", ", ").replace("\u2013", "-")
    normalized = normalized.replace(" -- ", "; ")
    normalized = re.sub(r"\s*;\s*", "; ", normalized)
    normalized = re.sub(r"\s+,", ",", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _normalize_salutation(text: str) -> str:
    salutation = _normalize_paragraph(text)
    if not salutation.lower().startswith("dear "):
        salutation = f"Dear {salutation}"
    if not salutation.endswith(","):
        salutation = f"{salutation},"
    return salutation


def _normalize_signoff(text: str) -> str:
    signoff = _normalize_whitespace(text)
    if not signoff:
        return "Sincerely,"
    if not signoff.endswith(","):
        signoff = f"{signoff},"
    return signoff


def _dedupe_preserve(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _ensure_id_references_exist(plan: CoverLetterPlan, profile: ResumeMasterProfile) -> None:
    bullet_ids = profile.all_bullet_ids()
    exp_ids = {exp.id for exp in profile.experience}
    edu_ids = {edu.id for edu in profile.education}

    unknown_bullets = sorted({x for x in plan.selected_bullet_ids if x not in bullet_ids})
    unknown_experience = sorted({x for x in plan.selected_experience_ids if x not in exp_ids})
    unknown_education = sorted({x for x in plan.selected_education_ids if x not in edu_ids})

    if unknown_bullets or unknown_experience or unknown_education:
        msg = (
            "Cover-letter plan references unknown profile IDs: "
            f"bullets={unknown_bullets}, experience={unknown_experience}, education={unknown_education}"
        )
        raise ValueError(msg)


def _ensure_no_placeholders(plan: CoverLetterPlan) -> None:
    text = "\n".join(
        [
            plan.salutation,
            plan.opening_paragraph,
            *plan.body_paragraphs,
            plan.closing_paragraph,
            plan.signoff,
            plan.signature_name,
        ]
    )
    lowered = text.lower()
    for pattern in _PLACEHOLDER_PATTERNS:
        if re.search(pattern, lowered):
            raise ValueError(f"Cover-letter plan contains placeholder pattern: {pattern}")


def _ensure_complete_sentences(plan: CoverLetterPlan) -> None:
    for para in [plan.opening_paragraph, *plan.body_paragraphs, plan.closing_paragraph]:
        if not para or para[-1] not in ".!?":
            raise ValueError("Cover-letter paragraph must end with terminal punctuation.")


def _ensure_no_duplicate_claims(plan: CoverLetterPlan) -> None:
    seen: set[str] = set()
    for paragraph in [plan.opening_paragraph, *plan.body_paragraphs, plan.closing_paragraph]:
        key = _normalized_claim_key(paragraph)
        if key in seen:
            raise ValueError("Cover-letter plan contains repeated claims.")
        seen.add(key)


def _normalized_claim_key(paragraph: str) -> str:
    no_punct = re.sub(r"[^a-z0-9\s]", "", paragraph.lower())
    tokens = [t for t in no_punct.split() if t not in {"the", "a", "an", "and", "to", "for"}]
    return " ".join(tokens)


def _ensure_company_reference_is_supported(plan: CoverLetterPlan, expected_company: str) -> None:
    if expected_company.strip().lower() not in plan.company_motivation.lower():
        raise ValueError("Cover-letter company_motivation must reference the target company.")


def _ensure_word_budget(plan: CoverLetterPlan) -> None:
    words = " ".join(
        [plan.opening_paragraph, *plan.body_paragraphs, plan.closing_paragraph]
    ).split()
    if len(words) > _MAX_WORDS:
        raise ValueError(f"Cover letter exceeds one-page target ({len(words)} words).")
    if len(words) < _MIN_WORDS:
        raise ValueError(f"Cover letter is too short for one-page target ({len(words)} words).")
