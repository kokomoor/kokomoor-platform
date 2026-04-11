"""Deterministic normalization and validation for cover letters."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from pipelines.job_agent.cover_letter.models import (
    CoverLetterDocument,
    CoverLetterPlan,
    RequirementEvidence,
)

if TYPE_CHECKING:
    from pipelines.job_agent.models.resume_tailoring import (
        CoverLetterPreferences,
        ResumeMasterProfile,
    )

logger = structlog.get_logger(__name__)

_PLACEHOLDER_PATTERNS = (
    r"\[company\]",
    r"\[hiring manager\]",
    r"\{\{.+?\}\}",
    r"\bTBD\b",
    r"<[^>]+>",
)

_MAX_WORDS = 420
_MIN_WORDS = 280
_SIMILARITY_THRESHOLD = 0.65
_COMPANY_MOTIVATION_MIN_WORDS = 10
_MIN_EVIDENCE_TOKENS_PER_ENTRY = 1
_AI_TELL_WARN_THRESHOLD = 3

# ── Banned phrase architecture ─────────────────────────────────────────
#
# Two-tier system informed by AI-detection research and the candidate's
# actual writing voice (reference letters: Anduril, Human Agency, CFS).
#
# Tier 1: CORE_BANNED_PHRASES — always rejected (hard failure).
#   Complete filler phrases that are unambiguously generic and never
#   appear in strong cover letters. Matched as case-insensitive substrings.
#
# Tier 2: AI_TELL_WORDS — warned when multiple co-occur.
#   Individual words statistically overrepresented in LLM output.
#   One occurrence is acceptable; density above _AI_TELL_WARN_THRESHOLD
#   generates a warning. Matched as whole words, case-insensitive.
#
# Profile-level banned phrases (from CoverLetterPreferences.banned_phrases)
# are ALSO hard failures — they represent the candidate's explicit voice
# constraints and should never be violated.

CORE_BANNED_PHRASES: tuple[str, ...] = (
    "i am excited to apply",
    "i am passionate about",
    "i am confident that my skills",
    "i am confident that my experience",
    "i believe i would be a great fit",
    "i would love the opportunity",
    "i am eager to bring my",
    "i bring a wealth of",
    "my unique blend of",
    "uniquely positioned",
    "unique opportunity",
    "proven track record",
    "results-driven",
    "detail-oriented",
    "self-starter",
    "team player",
    "hit the ground running",
    "think outside the box",
    "go above and beyond",
    "aligns perfectly with",
    "deeply resonates",
    "i am well-positioned",
    "i am thrilled",
    "i would be honored",
    "strong candidate for",
    "make me an ideal candidate",
    "valuable asset to your team",
    "extensive experience in",
    "strong foundation in",
    "well-rounded professional",
    "in today's fast-paced",
)

AI_TELL_WORDS: tuple[str, ...] = (
    "delve",
    "tapestry",
    "beacon",
    "synergy",
    "paradigm",
    "multifaceted",
    "unwavering",
    "stellar",
    "formidable",
    "cornerstone",
    "empower",
    "unleash",
    "underscore",
    "pivotal",
    "myriad",
    "plethora",
    "comprehensive",
    "furthermore",
    "moreover",
    "hone",
)

_GENERIC_OPENER_PATTERNS: tuple[str, ...] = (
    r"^i am writing to express my interest",
    r"^i am writing to apply for",
    r"^i wish to express my interest",
    r"^please accept this letter as my",
    r"^with great enthusiasm",
)

_EVIDENCE_STOPWORDS = frozenset(
    {
        "about",
        "above",
        "across",
        "after",
        "along",
        "among",
        "based",
        "being",
        "below",
        "between",
        "built",
        "could",
        "doing",
        "during",
        "every",
        "first",
        "found",
        "given",
        "great",
        "group",
        "happy",
        "helps",
        "ideas",
        "include",
        "including",
        "large",
        "later",
        "level",
        "local",
        "major",
        "makes",
        "model",
        "needs",
        "never",
        "often",
        "order",
        "other",
        "place",
        "point",
        "range",
        "right",
        "shall",
        "shown",
        "since",
        "small",
        "state",
        "still",
        "their",
        "there",
        "these",
        "thing",
        "those",
        "three",
        "times",
        "today",
        "total",
        "under",
        "until",
        "using",
        "value",
        "where",
        "which",
        "while",
        "whole",
        "would",
        "write",
        "years",
    }
)


@dataclass(frozen=True)
class CoverLetterValidationResult:
    """Validated and normalized cover-letter output."""

    plan: CoverLetterPlan
    document: CoverLetterDocument
    warnings: list[str] = field(default_factory=list)


def validate_cover_letter_plan(
    *,
    plan: CoverLetterPlan,
    profile: ResumeMasterProfile,
    expected_company: str,
    preferences: CoverLetterPreferences | None = None,
) -> CoverLetterValidationResult:
    """Validate, normalize, and convert an LLM plan into renderable structure.

    Hard failures (ValueError) are raised for structural issues that would
    produce an unusable letter. Soft violations are collected as warnings
    and returned alongside the result.
    """
    warnings: list[str] = []

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
            "requirement_evidence": [
                RequirementEvidence(
                    requirement=_normalize_paragraph(item.requirement),
                    supporting_bullet_ids=_dedupe_preserve(item.supporting_bullet_ids),
                )
                for item in plan.requirement_evidence
            ],
        }
    )

    if preferences is not None and preferences.preferred_signoff:
        normalized_plan.signoff = _normalize_signoff(preferences.preferred_signoff)

    # Hard checks — structural issues that produce unusable output.
    _ensure_id_references_exist(normalized_plan, profile)
    _ensure_evidence_mapping_consistency(normalized_plan)
    _ensure_no_placeholders(normalized_plan)
    _ensure_complete_sentences(normalized_plan)
    _ensure_minimum_evidence(normalized_plan)
    _ensure_no_banned_phrases(normalized_plan, preferences)
    _ensure_no_generic_opener(normalized_plan)
    _ensure_company_motivation_substance(normalized_plan)
    _ensure_prose_grounding(normalized_plan, profile)

    # Soft checks — quality issues that warrant warnings, not rejection.
    _warn_duplicate_claims(normalized_plan, warnings)
    _warn_company_reference(normalized_plan, expected_company, warnings)
    _warn_word_budget(normalized_plan, warnings)
    _warn_company_in_body(normalized_plan, expected_company, warnings)
    _warn_ai_tell_density(normalized_plan, warnings)
    _warn_motivation_body_overlap(normalized_plan, expected_company, warnings)

    if warnings:
        for w in warnings:
            logger.warning("cover_letter.validation_warning", warning=w)

    document = CoverLetterDocument(
        salutation=normalized_plan.salutation,
        opening_paragraph=normalized_plan.opening_paragraph,
        body_paragraphs=normalized_plan.body_paragraphs,
        closing_paragraph=normalized_plan.closing_paragraph,
        signoff=normalized_plan.signoff,
        signature_name=normalized_plan.signature_name,
    )
    return CoverLetterValidationResult(plan=normalized_plan, document=document, warnings=warnings)


# ── Normalization helpers ──────────────────────────────────────────────


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


# ── Hard checks (ValueError on failure) ───────────────────────────────


def _ensure_id_references_exist(plan: CoverLetterPlan, profile: ResumeMasterProfile) -> None:
    bullet_ids = profile.all_bullet_ids()
    exp_ids = {exp.id for exp in profile.experience}
    edu_ids = {edu.id for edu in profile.education}

    all_referenced_bullets = set(plan.selected_bullet_ids)
    for mapping in plan.requirement_evidence:
        all_referenced_bullets.update(mapping.supporting_bullet_ids)

    unknown_bullets = sorted(all_referenced_bullets - bullet_ids)
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


def _ensure_minimum_evidence(plan: CoverLetterPlan) -> None:
    if len(plan.selected_bullet_ids) < 2:
        raise ValueError("Cover letter must include at least two distinct evidence bullet IDs.")
    if not plan.selected_experience_ids:
        raise ValueError("Cover letter must include at least one selected experience ID.")


def _ensure_evidence_mapping_consistency(plan: CoverLetterPlan) -> None:
    if not plan.requirement_evidence:
        raise ValueError("Cover letter must include requirement_evidence mapping.")

    for mapping in plan.requirement_evidence:
        if not mapping.requirement:
            raise ValueError("Each requirement_evidence entry must include a requirement.")

    # selected_bullet_ids is derived: it is the union of all supporting_bullet_ids
    # across all requirement_evidence entries, plus any explicitly listed IDs.
    # This eliminates the failure mode where the model populates requirement_evidence
    # correctly but forgets to mirror the same IDs into the flat list.
    all_evidence_ids = {
        bid
        for mapping in plan.requirement_evidence
        for bid in mapping.supporting_bullet_ids
    }
    existing = set(plan.selected_bullet_ids)
    merged = list(plan.selected_bullet_ids) + sorted(all_evidence_ids - existing)
    plan.selected_bullet_ids[:] = list(dict.fromkeys(merged))  # stable dedup


def _ensure_no_banned_phrases(
    plan: CoverLetterPlan, preferences: CoverLetterPreferences | None
) -> None:
    """Hard-reject letters containing core or profile-level banned phrases."""
    text = _body_text(plan).lower()
    for phrase in CORE_BANNED_PHRASES:
        if phrase in text:
            raise ValueError(f"Cover letter contains banned phrase: '{phrase}'")

    if preferences is not None and preferences.banned_phrases:
        for phrase in preferences.banned_phrases:
            candidate = phrase.strip().lower()
            if candidate and candidate in text:
                raise ValueError(f"Cover letter contains profile-banned phrase: '{phrase}'")


def _ensure_no_generic_opener(plan: CoverLetterPlan) -> None:
    """Reject letters that open with a formulaic stock phrase."""
    opening_lower = plan.opening_paragraph.strip().lower()
    for pattern in _GENERIC_OPENER_PATTERNS:
        if re.match(pattern, opening_lower):
            raise ValueError(f"Opening paragraph uses a generic opener matching: {pattern}")


def _ensure_company_motivation_substance(plan: CoverLetterPlan) -> None:
    """Require company_motivation to contain actual reasoning, not just a name."""
    word_count = len(plan.company_motivation.split())
    if word_count < _COMPANY_MOTIVATION_MIN_WORDS:
        raise ValueError(
            f"company_motivation must contain at least {_COMPANY_MOTIVATION_MIN_WORDS} words "
            f"of specific reasoning (got {word_count})."
        )


def _ensure_prose_grounding(plan: CoverLetterPlan, profile: ResumeMasterProfile) -> None:
    """Verify that body paragraphs contain specific terms from cited evidence.

    For each requirement_evidence entry, extracts substantive tokens from the
    supporting bullets and checks that at least one appears in the letter body.
    Catches the failure mode where the LLM cites bullet IDs in metadata but
    writes generic prose that ignores the actual evidence.
    """
    body_lower = _body_text(plan).lower()

    ungrounded: list[str] = []
    for mapping in plan.requirement_evidence:
        all_evidence_tokens: set[str] = set()
        for bid in mapping.supporting_bullet_ids:
            bullet = profile.get_bullet(bid)
            if bullet is not None:
                all_evidence_tokens.update(_extract_evidence_tokens(bullet.text))

        if not all_evidence_tokens:
            continue

        found = sum(1 for token in all_evidence_tokens if token in body_lower)
        if found < _MIN_EVIDENCE_TOKENS_PER_ENTRY:
            ungrounded.append(mapping.requirement)

    if ungrounded:
        raise ValueError(
            "Letter body lacks specific evidence for requirement(s): "
            f"{ungrounded}. The prose must include concrete details from the "
            "cited profile bullets, not just generic claims."
        )


def _extract_evidence_tokens(text: str) -> set[str]:
    """Extract substantive tokens from a profile bullet for grounding checks.

    Targets tokens that carry specific meaning: numbers/metrics and
    non-trivial words (5+ chars, not in stopword set).
    """
    tokens: set[str] = set()
    for word in re.findall(r"[a-zA-Z0-9$%+/.]+", text):
        lower = word.lower().rstrip(".,;:")
        if not lower:
            continue
        if any(c.isdigit() for c in lower) or (
            len(lower) >= 5 and lower not in _EVIDENCE_STOPWORDS
        ):
            tokens.add(lower)
    return tokens


# ── Soft checks (warnings, not rejection) ─────────────────────────────


def _warn_duplicate_claims(plan: CoverLetterPlan, warnings: list[str]) -> None:
    paragraphs = [plan.opening_paragraph, *plan.body_paragraphs, plan.closing_paragraph]
    for i, para_a in enumerate(paragraphs):
        for para_b in paragraphs[i + 1 :]:
            similarity = _jaccard_similarity(para_a, para_b)
            if similarity > _SIMILARITY_THRESHOLD:
                warnings.append(
                    f"Paragraphs have high token overlap ({similarity:.0%}); "
                    "may contain repeated claims."
                )
                return


def _jaccard_similarity(text_a: str, text_b: str) -> float:
    tokens_a = _claim_tokens(text_a)
    tokens_b = _claim_tokens(text_b)
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union)


def _claim_tokens(paragraph: str) -> set[str]:
    no_punct = re.sub(r"[^a-z0-9\s]", "", paragraph.lower())
    stop_words = {"the", "a", "an", "and", "to", "for", "of", "in", "is", "that", "with", "i"}
    return {t for t in no_punct.split() if t not in stop_words}


def _warn_company_reference(
    plan: CoverLetterPlan, expected_company: str, warnings: list[str]
) -> None:
    if expected_company.strip().lower() not in plan.company_motivation.lower():
        warnings.append(
            f"company_motivation field does not reference the target company '{expected_company}'."
        )


def _warn_company_in_body(
    plan: CoverLetterPlan, expected_company: str, warnings: list[str]
) -> None:
    body_text = _body_text(plan).lower()
    if expected_company.strip().lower() not in body_text:
        warnings.append(f"Letter body does not mention the target company '{expected_company}'.")


def _warn_word_budget(plan: CoverLetterPlan, warnings: list[str]) -> None:
    count = len(_body_text(plan).split())
    if count > _MAX_WORDS:
        warnings.append(f"Cover letter exceeds target ({count} words, max {_MAX_WORDS}).")
    if count < _MIN_WORDS:
        warnings.append(f"Cover letter is short ({count} words, min {_MIN_WORDS}).")


def _warn_ai_tell_density(plan: CoverLetterPlan, warnings: list[str]) -> None:
    """Warn when multiple AI-overused words co-occur in the letter body."""
    body_lower = _body_text(plan).lower()
    found = [word for word in AI_TELL_WORDS if re.search(rf"\b{word}\b", body_lower)]
    if len(found) >= _AI_TELL_WARN_THRESHOLD:
        warnings.append(
            f"Letter contains {len(found)} AI-tell words ({', '.join(found)}); "
            "may read as LLM-generated."
        )


def _warn_motivation_body_overlap(
    plan: CoverLetterPlan, expected_company: str, warnings: list[str]
) -> None:
    """Warn if the company-specific reasoning in company_motivation is absent from the body."""
    motivation_tokens = _extract_evidence_tokens(plan.company_motivation)
    company_tokens = {w.lower() for w in expected_company.split() if len(w) >= 3}
    motivation_tokens -= company_tokens

    if not motivation_tokens:
        return

    body_lower = _body_text(plan).lower()
    found = sum(1 for t in motivation_tokens if t in body_lower)
    if found < 2 and len(motivation_tokens) >= 2:
        warnings.append(
            "company_motivation reasoning is not reflected in the letter body. "
            "The body should incorporate the company-specific argument."
        )


# ── Helpers ────────────────────────────────────────────────────────────


def _body_text(plan: CoverLetterPlan) -> str:
    """Concatenate all prose paragraphs."""
    return " ".join([plan.opening_paragraph, *plan.body_paragraphs, plan.closing_paragraph])
