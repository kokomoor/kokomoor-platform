"""Deterministic form-field → profile mapping.

Pure-Python mapping from a form field (label + type + options) to a
value drawn from :class:`CandidateApplicationProfile`. No LLM calls, no
network, no async — the mapper runs in microseconds and is the first
thing the application engine consults for every field.

The mapper handles the 80-90% of application form fields that are
deterministically answerable from the profile: name, contact, address,
work authorization, education, salary expectations, and EEO responses.
Anything it can't answer returns ``confidence=0.0``; the caller then
routes that field to the LLM QA answerer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from pipelines.job_agent.models.application import CandidateApplicationProfile


@dataclass(frozen=True)
class FieldMapping:
    """Result of a deterministic field-mapping attempt.

    ``confidence`` is a rough self-assessment used by the caller to
    decide whether to trust the value or escalate to the LLM:

    - ``1.0`` — exact label match from ``_FIELD_PATTERNS``
    - ``0.8`` — substring label match (e.g. "Your phone number *")
    - ``0.5`` — decline-style fallback picked for a select field with
      no matching option
    - ``0.3`` — deterministic value present but no option matched and
      no decline fallback available
    - ``0.0`` — no deterministic mapping found; caller should route to
      the LLM QA answerer
    """

    value: str
    confidence: float
    source: str


# ---------- Pattern table ----------

# Each entry is ``label_token → (source_tag, getter)``. The label token is
# the normalized form (lowercased, punctuation stripped, whitespace
# collapsed) of a common form-field label. ``source_tag`` identifies the
# profile section the value came from and is propagated on the returned
# :class:`FieldMapping` so the caller / tests can assert provenance.
_PatternHandler = tuple[str, "Callable[[CandidateApplicationProfile], str]"]

_FIELD_PATTERNS: dict[str, _PatternHandler] = {
    # Personal
    "first name": ("personal", lambda p: p.personal.first_name),
    "last name": ("personal", lambda p: p.personal.last_name),
    "full name": (
        "personal",
        lambda p: f"{p.personal.first_name} {p.personal.last_name}",
    ),
    "name": ("personal", lambda p: f"{p.personal.first_name} {p.personal.last_name}"),
    "email": ("personal", lambda p: p.personal.email),
    "phone": ("personal", lambda p: p.personal.phone_formatted),
    "linkedin": ("personal", lambda p: p.personal.linkedin_url),
    "github": ("personal", lambda p: p.personal.github_url),
    "portfolio": ("personal", lambda p: p.personal.portfolio_url),
    "website": ("personal", lambda p: p.personal.website_url),
    # Address
    "city": ("address", lambda p: p.address.city),
    "state": ("address", lambda p: p.address.state),
    "zip": ("address", lambda p: p.address.zip),
    "country": ("address", lambda p: p.address.country),
    # Authorization
    "authorized to work": (
        "authorization",
        lambda p: "Yes" if p.authorization.authorized_us else "No",
    ),
    "work authorization": (
        "authorization",
        lambda p: "Yes" if p.authorization.authorized_us else "No",
    ),
    "sponsorship": (
        "authorization",
        lambda p: "Yes" if p.authorization.require_sponsorship else "No",
    ),
    "require sponsorship": (
        "authorization",
        lambda p: "Yes" if p.authorization.require_sponsorship else "No",
    ),
    "visa": (
        "authorization",
        lambda p: "Yes" if p.authorization.require_sponsorship else "No",
    ),
    # Education
    "degree": ("education", lambda p: p.education.highest_degree),
    "school": ("education", lambda p: p.education.school),
    "university": ("education", lambda p: p.education.school),
    "graduation": ("education", lambda p: p.education.graduation_year),
    "gpa": ("education", lambda p: p.education.gpa),
    "field of study": ("education", lambda p: p.education.field_of_study),
    "major": ("education", lambda p: p.education.field_of_study),
    # Screening
    "years of experience": ("screening", lambda p: p.screening.years_experience),
    "years experience": ("screening", lambda p: p.screening.years_experience),
    "relocate": (
        "screening",
        lambda p: "Yes" if p.screening.willing_to_relocate else "No",
    ),
    "salary": ("screening", lambda p: p.screening.desired_salary),
    "compensation": ("screening", lambda p: p.screening.desired_salary),
    "how did you hear": ("source", lambda p: p.source.default),
    "how did you find": ("source", lambda p: p.source.default),
    # Demographics
    "gender": ("demographics", lambda p: p.demographics.gender),
    "race": ("demographics", lambda p: p.demographics.race_ethnicity),
    "ethnicity": ("demographics", lambda p: p.demographics.race_ethnicity),
    "veteran": ("demographics", lambda p: p.demographics.veteran_status),
    "disability": ("demographics", lambda p: p.demographics.disability_status),
}

# Longest keys first so "first name" beats "name" on substring match.
_PATTERN_KEYS_BY_LENGTH: list[str] = sorted(
    _FIELD_PATTERNS.keys(),
    key=len,
    reverse=True,
)

_DECLINE_MARKERS: tuple[str, ...] = (
    "decline",
    "prefer not",
    "don't wish",
    "dont wish",
    "do not wish",
    "i don't want",
    "i dont want",
)

_FUZZY_MIN_RATIO = 0.6

_UNMAPPED = FieldMapping(value="", confidence=0.0, source="unmapped")


# ---------- Helpers ----------


def _normalize_label(label: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace.

    ``"FIRST NAME *"`` → ``"first name"``.
    """
    lowered = label.lower()
    depunct = re.sub(r"[^\w\s]", " ", lowered)
    return " ".join(depunct.split())


def _fuzzy_match_option(value: str, options: Sequence[str]) -> str | None:
    """Return the best matching option for ``value`` or ``None``.

    Exact (case-insensitive) match wins outright. Otherwise, compute
    ``SequenceMatcher`` ratios between ``value`` and each option and
    return the highest-scoring option if it clears
    :data:`_FUZZY_MIN_RATIO`.
    """
    if not value or not options:
        return None
    value_lower = value.lower()
    for opt in options:
        if opt.lower() == value_lower:
            return opt
    best_ratio = 0.0
    best_option: str | None = None
    for opt in options:
        ratio = SequenceMatcher(None, value_lower, opt.lower()).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_option = opt
    if best_option is not None and best_ratio >= _FUZZY_MIN_RATIO:
        return best_option
    return None


def _find_decline_option(options: Sequence[str]) -> str | None:
    """Return the first option whose text reads like a decline-to-answer."""
    for opt in options:
        lower = opt.lower()
        if any(marker in lower for marker in _DECLINE_MARKERS):
            return opt
    return None


def _apply_options(
    value: str,
    options: Sequence[str] | None,
    *,
    confidence: float,
    source: str,
) -> FieldMapping:
    """Resolve a deterministic value against a select/radio option list.

    Free-text fields (``options`` is ``None`` or empty) pass the value
    through unchanged. Select-style fields prefer a fuzzy option match,
    fall back to a decline option if one exists, and finally return the
    raw value with reduced confidence as a last resort.
    """
    if not options:
        return FieldMapping(value=value, confidence=confidence, source=source)

    matched = _fuzzy_match_option(value, options)
    if matched is not None:
        return FieldMapping(value=matched, confidence=confidence, source=source)

    decline = _find_decline_option(options)
    if decline is not None:
        return FieldMapping(
            value=decline,
            confidence=min(confidence, 0.5),
            source="decline_fallback",
        )

    return FieldMapping(
        value=value,
        confidence=max(confidence - 0.5, 0.3),
        source=source,
    )


# ---------- Public API ----------


def map_field(
    label: str,
    field_type: str,
    options: Sequence[str] | None,
    profile: CandidateApplicationProfile,
) -> FieldMapping:
    """Map a form field to a candidate profile value.

    Algorithm:

    1. Normalize the label.
    2. Exact match against :data:`_FIELD_PATTERNS` → confidence 1.0.
    3. Substring match (longest key first) → confidence 0.8.
    4. For select/radio fields, reconcile the value against ``options``
       via :func:`_apply_options`.
    5. Otherwise, return :data:`_UNMAPPED` so the caller can escalate
       to the LLM QA answerer.

    Args:
        label: Raw field label as shown on the form.
        field_type: HTML field type (``"text"``, ``"select"``, etc.).
            Accepted for API symmetry; option reconciliation is gated
            on ``options`` being non-empty, not on the type string.
        options: Available options for select/radio/checkbox fields;
            ``None`` or empty for free-text fields.
        profile: The loaded :class:`CandidateApplicationProfile`.

    Returns:
        A :class:`FieldMapping` with the matched value, a confidence
        score, and the originating profile section.
    """
    normalized = _normalize_label(label)

    handler = _FIELD_PATTERNS.get(normalized)
    if handler is not None:
        source, getter = handler
        return _apply_options(
            getter(profile),
            options,
            confidence=1.0,
            source=source,
        )

    for key in _PATTERN_KEYS_BY_LENGTH:
        if key in normalized:
            source, getter = _FIELD_PATTERNS[key]
            return _apply_options(
                getter(profile),
                options,
                confidence=0.8,
                source=source,
            )

    return _UNMAPPED
