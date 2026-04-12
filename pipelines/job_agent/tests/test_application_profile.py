"""Tests for the candidate application profile loader."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from pipelines.job_agent.models import (
    CandidateApplicationProfile,
    load_application_profile,
)
from pipelines.job_agent.models.application import _load_cached

_EXAMPLE_PATH = (
    Path(__file__).resolve().parents[1] / "context" / "candidate_application.example.yaml"
)


@pytest.fixture(autouse=True)
def _clear_profile_cache() -> None:
    """Clear the per-path profile cache between tests."""
    _load_cached.cache_clear()


def test_loads_every_section() -> None:
    """Every nested section of the example profile parses into its model."""
    profile = load_application_profile(_EXAMPLE_PATH)

    assert isinstance(profile, CandidateApplicationProfile)
    assert profile.schema_version == 1

    assert profile.personal.first_name == "Jane"
    assert profile.personal.last_name == "Doe"
    assert profile.personal.email == "jane.doe@example.com"
    assert profile.personal.phone.startswith("+")
    assert profile.personal.phone_formatted.startswith("(")
    assert profile.personal.linkedin_url.startswith("https://")

    assert profile.address.city == "Exampletown"
    assert profile.address.state == "CA"
    assert profile.address.country == "United States"

    assert profile.authorization.authorized_us is True
    assert profile.authorization.require_sponsorship is False
    assert profile.authorization.citizenship == "US Citizen"

    assert profile.demographics.gender
    assert profile.demographics.race_ethnicity
    assert profile.demographics.veteran_status
    assert profile.demographics.disability_status

    assert profile.education.highest_degree == "BS"
    assert profile.education.school == "Example University"
    assert profile.education.field_of_study == "Computer Science"
    assert profile.education.additional_degrees == []

    assert profile.screening.years_experience == "5"
    assert profile.screening.willing_to_relocate is True
    assert profile.screening.desired_salary == "175000"
    assert profile.screening.languages_spoken[0].language == "English"

    assert profile.source.default == "Online job search"
    assert profile.source.linkedin == "LinkedIn"


def test_loader_caches_by_path() -> None:
    """Two loads of the same resolved path return the same instance."""
    first = load_application_profile(_EXAMPLE_PATH)
    second = load_application_profile(_EXAMPLE_PATH)
    assert first is second


def test_missing_file_raises(tmp_path: Path) -> None:
    """A non-existent path raises ``FileNotFoundError`` with a helpful message."""
    missing = tmp_path / "does_not_exist.yaml"
    with pytest.raises(FileNotFoundError, match="candidate_application"):
        load_application_profile(missing)


def test_missing_required_field_raises_validation_error(tmp_path: Path) -> None:
    """Dropping ``personal.email`` fails Pydantic validation."""
    bad = tmp_path / "bad_profile.yaml"
    bad.write_text(
        """\
schema_version: 1
personal:
  first_name: Jane
  last_name: Doe
  phone: "+15555550123"
  phone_formatted: "(555) 555-0123"
address:
  city: Exampletown
  state: CA
authorization:
  authorized_us: true
  require_sponsorship: false
demographics:
  gender: "Decline to self-identify"
  race_ethnicity: "Decline to self-identify"
  veteran_status: "I am not a protected veteran"
  disability_status: "I do not wish to answer"
education:
  highest_degree: BS
  school: Example University
  graduation_year: "2020"
  field_of_study: Computer Science
screening:
  years_experience: "5"
  willing_to_relocate: true
  desired_salary: "175000"
  how_did_you_hear: "Online job search"
source:
  default: "Online job search"
""",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_application_profile(bad)


def test_non_mapping_yaml_raises_value_error(tmp_path: Path) -> None:
    """A top-level list (not a mapping) is rejected with ``ValueError``."""
    bad = tmp_path / "list_profile.yaml"
    bad.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
    with pytest.raises(ValueError, match="did not parse as a mapping"):
        load_application_profile(bad)
