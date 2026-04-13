"""Enhanced LLM question answerer.

Uses ``structured_complete`` with job context, character-limit awareness,
and a run-scoped cache for generic questions.
"""

from __future__ import annotations

import functools
import hashlib
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

import structlog
from pydantic import BaseModel, Field

from core.llm.structured import structured_complete

if TYPE_CHECKING:
    from core.llm.protocol import LLMClient

logger = structlog.get_logger(__name__)


class FormFieldAnswer(BaseModel):
    """LLM-generated answer for a single form field."""

    answer: str = Field(description="The value to fill into the field.")
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Self-assessed confidence that this is the correct answer.",
    )
    source: str = Field(description="Which section of the candidate profile this answer came from.")
    used_cache: bool = False


# --- Scoped Cache ---

class QACache:
    """Issue 12: Context manager for scoped QA caching to prevent privacy leaks."""

    def __init__(self) -> None:
        self._cache: Dict[str, FormFieldAnswer] = {}

    def get(self, key: str) -> Optional[FormFieldAnswer]:
        return self._cache.get(key)

    def set(self, key: str, answer: FormFieldAnswer) -> None:
        self._cache[key] = answer

    def clear(self) -> None:
        self._cache.clear()


# --- Safe Interpolation ---

def _safe_format(template: str, **kwargs: Any) -> str:
    """Issue 7: Safely interpolate {placeholders} in a template.
    
    Avoids KeyError from literal braces in markdown/CSS.
    """
    def replacer(match: re.Match[str]) -> str:
        key = match.group(1)
        # If the key exists in kwargs, replace it; otherwise, leave as is
        return str(kwargs.get(key, match.group(0)))

    return re.sub(r"\{(\w+)\}", replacer, template)


# --- Profile Pruning ---

def _prune_profile_for_field(profile_json: str, field_label: str) -> str:
    """Issue 13: Reduce token bloat by sending only relevant profile sections."""
    try:
        data = json.loads(profile_json)
    except Exception:
        return profile_json

    label = field_label.lower()
    sections = ["personal"]

    if any(k in label for k in ["gender", "race", "ethnicity", "veteran", "disability", "eeo"]):
        sections.append("demographics")
    if any(k in label for k in ["school", "university", "college", "degree", "graduation", "gpa", "study", "major"]):
        sections.append("education")
    if any(k in label for k in ["address", "city", "state", "zip", "country"]):
        sections.append("address")
    if any(k in label for k in ["work", "authorized", "sponsorship", "visa", "citizen", "clearance"]):
        sections.append("authorization")
    if any(k in label for k in ["experience", "salary", "compensation", "relocate", "start date", "hear about"]):
        sections.append("screening")

    # If the label is extremely generic, we might want everything, but 
    # the heuristic above handles most form field types.
    if len(sections) == 1 and len(label) > 15:
        return profile_json

    pruned = {k: data.get(k) for k in sections if k in data}
    return json.dumps(pruned, indent=2)


@functools.lru_cache(maxsize=1)
def _load_qa_system_prompt() -> str:
    path = Path(__file__).parent / "prompts" / "qa_system.md"
    if not path.exists():
        return (
            "You are a job application assistant. Given a candidate profile and "
            "a form field from a job application, determine the correct answer."
        )
    return path.read_text(encoding="utf-8")


def clear_qa_cache() -> None:
    """Clear the run-scoped QA answer cache."""
    _QA_CACHE.clear()


def _is_generic_question(label: str) -> bool:
    """Questions whose answer doesn't change per listing."""
    generic_patterns = [
        "authorized to work",
        "sponsorship",
        "visa",
        "how did you hear",
        "gender",
        "race",
        "veteran",
        "disability",
        "relocate",
        "years of experience",
        "salary",
        "start date",
    ]
    label_lower = label.lower()
    return any(p in label_lower for p in generic_patterns)


def _get_cache_key(
    field_label: str,
    field_type: str,
    maxlength: int | None,
    field_options: list[str] | None,
) -> str:
    """Generate a stable cache key for a question."""
    normalized_label = field_label.lower().strip()
    options_hash = ""
    if field_options:
        options_hash = hashlib.sha256(str(sorted(field_options)).encode()).hexdigest()

    return f"{normalized_label}|{field_type}|{maxlength}|{options_hash}"


async def answer_application_question(
    llm: LLMClient,
    *,
    field_label: str,
    field_type: str,
    field_options: list[str] | None = None,
    candidate_profile: str,
    job_title: str = "",
    company: str = "",
    job_analysis: str = "",
    cover_letter_text: str = "",
    maxlength: int | None = None,
    run_id: str = "",
    cache: Optional[QACache] = None,
) -> FormFieldAnswer:
    """Answer a job application question with full job context and caching."""
    is_generic = _is_generic_question(field_label)
    cache_key = _get_cache_key(field_label, field_type, maxlength, field_options)

    if is_generic and cache:
        cached = cache.get(cache_key)
        if cached:
            result = cached.model_copy()
            result.used_cache = True
            return result

    system_template = _load_qa_system_prompt()
    options_text = ", ".join(field_options) if field_options else "None"

    # Issue 13: Prune profile to reduce tokens
    pruned_profile = _prune_profile_for_field(candidate_profile, field_label)

    # Issue 7: Safe interpolation
    prompt = _safe_format(
        system_template,
        job_title=job_title or "this role",
        company=company or "this company",
        candidate_profile=pruned_profile,
        job_analysis=job_analysis or "Not provided.",
        cover_letter_text=cover_letter_text or "Not provided.",
        field_label=field_label,
        field_type=field_type,
        field_options=options_text,
        maxlength=str(maxlength) if maxlength else "Not specified",
    )

    result = await structured_complete(
        llm,
        prompt,
        response_model=FormFieldAnswer,
        run_id=run_id,
    )

    if is_generic and cache:
        cache.set(cache_key, result)

    return result


async def answer_form_field(
    llm: LLMClient,
    *,
    field_label: str,
    field_type: str,
    field_options: list[str] | None = None,
    candidate_profile: str,
    run_id: str = "",
) -> FormFieldAnswer:
    """Backwards-compatible wrapper for answer_application_question."""
    return await answer_application_question(
        llm=llm,
        field_label=field_label,
        field_type=field_type,
        field_options=field_options,
        candidate_profile=candidate_profile,
        run_id=run_id,
    )
