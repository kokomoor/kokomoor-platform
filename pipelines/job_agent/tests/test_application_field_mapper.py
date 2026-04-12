"""Tests for the deterministic form-field mapper."""

from __future__ import annotations

from pathlib import Path

import pytest

from pipelines.job_agent.application.field_mapper import (
    _FIELD_PATTERNS,
    FieldMapping,
    _fuzzy_match_option,
    _normalize_label,
    map_field,
)
from pipelines.job_agent.models import load_application_profile
from pipelines.job_agent.models.application import _load_cached

_EXAMPLE_PATH = (
    Path(__file__).resolve().parents[1] / "context" / "candidate_application.example.yaml"
)


@pytest.fixture(autouse=True)
def _clear_profile_cache() -> None:
    _load_cached.cache_clear()


@pytest.fixture
def profile() -> object:
    return load_application_profile(_EXAMPLE_PATH)


# ---------------------------------------------------------------------------
# Label normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("First Name", "first name"),
        ("FIRST NAME *", "first name"),
        ("  First   Name  ", "first name"),
        ("First-Name:", "first name"),
        ("First_Name", "first_name"),  # underscores count as word chars
        ("E-mail Address", "e mail address"),
    ],
)
def test_normalize_label(raw: str, expected: str) -> None:
    assert _normalize_label(raw) == expected


# ---------------------------------------------------------------------------
# Exact-match table: one case per entry in _FIELD_PATTERNS
# ---------------------------------------------------------------------------


_EXACT_CASES: list[tuple[str, str, str]] = [
    # (label, expected source tag, expected value produced from the
    #  example profile fixture)
    ("First Name", "personal", "Jane"),
    ("Last Name", "personal", "Doe"),
    ("Full Name", "personal", "Jane Doe"),
    ("Name", "personal", "Jane Doe"),
    ("Email", "personal", "jane.doe@example.com"),
    ("Phone", "personal", "(555) 555-0123"),
    ("LinkedIn", "personal", "https://www.linkedin.com/in/jane-doe-example/"),
    ("GitHub", "personal", "https://github.com/jane-doe-example"),
    ("Portfolio", "personal", ""),
    ("Website", "personal", ""),
    ("City", "address", "Exampletown"),
    ("State", "address", "CA"),
    ("Zip", "address", "94000"),
    ("Country", "address", "United States"),
    ("Authorized to work", "authorization", "Yes"),
    ("Work authorization", "authorization", "Yes"),
    ("Sponsorship", "authorization", "No"),
    ("Require sponsorship", "authorization", "No"),
    ("Visa", "authorization", "No"),
    ("Degree", "education", "BS"),
    ("School", "education", "Example University"),
    ("University", "education", "Example University"),
    ("Graduation", "education", "2020"),
    ("GPA", "education", ""),
    ("Field of Study", "education", "Computer Science"),
    ("Major", "education", "Computer Science"),
    ("Years of Experience", "screening", "5"),
    ("Years Experience", "screening", "5"),
    ("Relocate", "screening", "Yes"),
    ("Salary", "screening", "175000"),
    ("Compensation", "screening", "175000"),
    ("How did you hear", "source", "Online job search"),
    ("How did you find", "source", "Online job search"),
    ("Gender", "demographics", "Decline to self-identify"),
    ("Race", "demographics", "Decline to self-identify"),
    ("Ethnicity", "demographics", "Decline to self-identify"),
    ("Veteran", "demographics", "I am not a protected veteran"),
    ("Disability", "demographics", "I do not wish to answer"),
]


def test_exact_cases_cover_every_pattern() -> None:
    """Guardrail: every entry in _FIELD_PATTERNS is exercised above."""
    covered = {_normalize_label(label) for label, _, _ in _EXACT_CASES}
    assert covered == set(_FIELD_PATTERNS.keys())


@pytest.mark.parametrize(("label", "expected_source", "expected_value"), _EXACT_CASES)
def test_exact_label_maps_to_profile_section(
    profile: object,
    label: str,
    expected_source: str,
    expected_value: str,
) -> None:
    result = map_field(label, "text", None, profile)  # type: ignore[arg-type]
    assert result.source == expected_source
    assert result.value == expected_value
    assert result.confidence == 1.0


# ---------------------------------------------------------------------------
# Substring match + case insensitivity
# ---------------------------------------------------------------------------


def test_substring_match_lower_confidence(profile: object) -> None:
    result = map_field("Your phone number *", "text", None, profile)  # type: ignore[arg-type]
    assert result.source == "personal"
    assert result.value == "(555) 555-0123"
    assert result.confidence == pytest.approx(0.8)


def test_case_insensitive_label(profile: object) -> None:
    result = map_field("FIRST NAME *", "text", None, profile)  # type: ignore[arg-type]
    assert result.source == "personal"
    assert result.value == "Jane"
    assert result.confidence == 1.0


def test_longer_substring_beats_shorter(profile: object) -> None:
    """'Your first name please' should resolve to first_name, not name."""
    result = map_field("Your first name please", "text", None, profile)  # type: ignore[arg-type]
    assert result.source == "personal"
    assert result.value == "Jane"


# ---------------------------------------------------------------------------
# Select / radio / option handling
# ---------------------------------------------------------------------------


def test_select_exact_option_match(profile: object) -> None:
    options = ["Yes", "No"]
    result = map_field("Work authorization", "select", options, profile)  # type: ignore[arg-type]
    assert result.value == "Yes"
    assert result.confidence == 1.0


def test_select_fuzzy_option_match(profile: object) -> None:
    """'Male/Man' option should be chosen when profile says 'Male'."""
    # Override the example profile gender to a concrete value for this case.
    profile.demographics.gender = "Male"  # type: ignore[attr-defined]
    options = ["Male/Man", "Female/Woman", "Non-Binary", "Decline to self-identify"]
    result = map_field("Gender", "select", options, profile)  # type: ignore[arg-type]
    assert result.value == "Male/Man"
    assert result.source == "demographics"
    assert result.confidence == 1.0


def test_select_decline_fallback_when_no_match(profile: object) -> None:
    """Profile value not in options → pick the decline option."""
    profile.demographics.race_ethnicity = "Klingon"  # type: ignore[attr-defined]
    options = [
        "White",
        "Black or African American",
        "Hispanic or Latino",
        "Asian",
        "Decline to self-identify",
    ]
    result = map_field("Race", "select", options, profile)  # type: ignore[arg-type]
    assert result.value == "Decline to self-identify"
    assert result.source == "decline_fallback"
    assert result.confidence == pytest.approx(0.5)


def test_select_decline_fallback_recognizes_prefer_not(profile: object) -> None:
    profile.demographics.gender = "Attack Helicopter"  # type: ignore[attr-defined]
    options = ["Male", "Female", "Non-Binary", "Prefer not to say"]
    result = map_field("Gender", "select", options, profile)  # type: ignore[arg-type]
    assert result.value == "Prefer not to say"
    assert result.source == "decline_fallback"


def test_select_no_match_no_decline_returns_reduced_confidence(
    profile: object,
) -> None:
    profile.demographics.gender = "Attack Helicopter"  # type: ignore[attr-defined]
    options = ["Male", "Female", "Non-Binary"]
    result = map_field("Gender", "select", options, profile)  # type: ignore[arg-type]
    # No fuzzy match, no decline — fall through to raw value with reduced confidence.
    assert result.value == "Attack Helicopter"
    assert result.confidence == pytest.approx(0.5)
    assert result.source == "demographics"


# ---------------------------------------------------------------------------
# Unknown label
# ---------------------------------------------------------------------------


def test_unknown_label_is_unmapped(profile: object) -> None:
    result = map_field("Favorite color", "text", None, profile)  # type: ignore[arg-type]
    assert result == FieldMapping(value="", confidence=0.0, source="unmapped")


def test_unknown_label_with_options_is_unmapped(profile: object) -> None:
    options = ["Red", "Blue", "Green", "Decline to self-identify"]
    result = map_field("Favorite color", "select", options, profile)  # type: ignore[arg-type]
    assert result.confidence == 0.0
    assert result.source == "unmapped"


# ---------------------------------------------------------------------------
# _fuzzy_match_option unit tests
# ---------------------------------------------------------------------------


def test_fuzzy_match_exact_insensitive() -> None:
    assert _fuzzy_match_option("yes", ["Yes", "No"]) == "Yes"


def test_fuzzy_match_below_threshold_returns_none() -> None:
    assert _fuzzy_match_option("foo", ["bar", "baz", "qux"]) is None


def test_fuzzy_match_empty_inputs_return_none() -> None:
    assert _fuzzy_match_option("", ["Yes"]) is None
    assert _fuzzy_match_option("Yes", []) is None
