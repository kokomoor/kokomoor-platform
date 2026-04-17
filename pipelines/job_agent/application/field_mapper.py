"""Deterministic form-field → profile mapping.

Pure-Python mapping from a form field (label + type + options) to a
value drawn from :class:`CandidateApplicationProfile`. No LLM calls, no
network, no async — the mapper runs in microseconds and is the first
thing the application engine consults for every field.

The patterns are loaded from ``field_patterns.yaml`` at runtime, enabling
extensibility without code changes.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import yaml

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from pipelines.job_agent.models.application import CandidateApplicationProfile


@dataclass(frozen=True)
class FieldMapping:
    """Result of a deterministic field-mapping attempt.

    ``confidence`` is a rough self-assessment used by the caller to
    decide whether to trust the value or escalate to the LLM.
    """

    value: str
    confidence: float
    source: str


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


@functools.lru_cache(maxsize=1)
def _load_patterns(path: Path) -> dict[str, tuple[str, str]]:
    """Load and flatten the nested YAML patterns into a lookup table."""
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    patterns: dict[str, tuple[str, str]] = {}
    if not raw:
        return patterns

    for section, section_patterns in raw.items():
        if not isinstance(section_patterns, dict):
            continue
        for label, getter_key in section_patterns.items():
            patterns[label] = (section, getter_key)
    return patterns


class FieldMapper:
    """Loads and applies deterministic field mapping patterns."""

    _GETTERS: ClassVar[dict[str, Callable[[CandidateApplicationProfile], str]]] = {
        "personal.first_name": lambda p: p.personal.first_name,
        "personal.last_name": lambda p: p.personal.last_name,
        "personal.full_name": lambda p: f"{p.personal.first_name} {p.personal.last_name}",
        "personal.email": lambda p: p.personal.email,
        "personal.phone_formatted": lambda p: p.personal.phone_formatted,
        "personal.linkedin_url": lambda p: p.personal.linkedin_url,
        "personal.github_url": lambda p: p.personal.github_url,
        "personal.portfolio_url": lambda p: p.personal.portfolio_url,
        "personal.website_url": lambda p: p.personal.website_url,
        "address.city": lambda p: p.address.city,
        "address.state": lambda p: p.address.state,
        "address.zip": lambda p: p.address.zip,
        "address.country": lambda p: p.address.country,
        "authorization.authorized_us": lambda p: "Yes" if p.authorization.authorized_us else "No",
        "authorization.require_sponsorship": lambda p: "Yes" if p.authorization.require_sponsorship else "No",
        "education.highest_degree": lambda p: p.education.highest_degree,
        "education.school": lambda p: p.education.school,
        "education.graduation_year": lambda p: p.education.graduation_year,
        "education.gpa": lambda p: p.education.gpa,
        "education.field_of_study": lambda p: p.education.field_of_study,
        "screening.years_experience": lambda p: p.screening.years_experience,
        "screening.willing_to_relocate": lambda p: "Yes" if p.screening.willing_to_relocate else "No",
        "screening.desired_salary": lambda p: p.screening.desired_salary,
        "source.default": lambda p: p.source.default,
        "demographics.gender": lambda p: p.demographics.gender,
        "demographics.race_ethnicity": lambda p: p.demographics.race_ethnicity,
        "demographics.veteran_status": lambda p: p.demographics.veteran_status,
        "demographics.disability_status": lambda p: p.demographics.disability_status,
    }

    def __init__(self, patterns_path: Path | None = None) -> None:
        if patterns_path is None:
            patterns_path = Path(__file__).parent / "field_patterns.yaml"

        self._patterns = _load_patterns(patterns_path)
        self._pattern_keys_by_length = sorted(
            self._patterns.keys(),
            key=len,
            reverse=True,
        )

    def _normalize_label(self, label: str) -> str:
        lowered = label.lower()
        depunct = re.sub(r"[^\w\s]", " ", lowered)
        return " ".join(depunct.split())

    def _fuzzy_match_option(self, value: str, options: Sequence[str]) -> str | None:
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

    def _find_decline_option(self, options: Sequence[str]) -> str | None:
        for opt in options:
            lower = opt.lower()
            if any(marker in lower for marker in _DECLINE_MARKERS):
                return opt
        return None

    def _apply_options(
        self,
        value: str,
        options: Sequence[str] | None,
        *,
        confidence: float,
        source: str,
    ) -> FieldMapping:
        if not options:
            return FieldMapping(value=value, confidence=confidence, source=source)

        matched = self._fuzzy_match_option(value, options)
        if matched is not None:
            return FieldMapping(value=matched, confidence=confidence, source=source)

        decline = self._find_decline_option(options)
        if decline is not None:
            return FieldMapping(
                value=decline,
                confidence=min(confidence, 0.5),
                source="decline_fallback",
            )

        # FIX: If we have options and no match/decline found, do NOT return the raw value
        # as it will likely fail form validation. Return unmapped for LLM escalation.
        return _UNMAPPED

    def map_field(
        self,
        label: str,
        field_type: str,
        options: Sequence[str] | None,
        profile: CandidateApplicationProfile,
    ) -> FieldMapping:
        """Map a form field to a candidate profile value."""
        normalized = self._normalize_label(label)

        # 1. Exact match
        pattern = self._patterns.get(normalized)
        if pattern:
            section, getter_key = pattern
            getter = self._GETTERS.get(getter_key)
            if getter:
                return self._apply_options(
                    getter(profile),
                    options,
                    confidence=1.0,
                    source=section,
                )

        # 2. Substring match
        for key in self._pattern_keys_by_length:
            if key in normalized:
                section, getter_key = self._patterns[key]
                getter = self._GETTERS.get(getter_key)
                if getter:
                    return self._apply_options(
                        getter(profile),
                        options,
                        confidence=0.8,
                        source=section,
                    )

        return _UNMAPPED


# Backward compatible singleton interface
_MAPPER: FieldMapper | None = None

def map_field(
    label: str,
    field_type: str,
    options: Sequence[str] | None,
    profile: CandidateApplicationProfile,
) -> FieldMapping:
    """Global singleton entrypoint for field mapping."""
    global _MAPPER
    if _MAPPER is None:
        _MAPPER = FieldMapper()
    return _MAPPER.map_field(label, field_type, options, profile)
