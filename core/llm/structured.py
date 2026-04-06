"""Structured output helpers for LLM responses.

Provides utilities to request and validate structured (JSON/Pydantic)
outputs from Claude. Uses prompt engineering to coerce JSON output,
then validates against a Pydantic model.

Usage:
    from core.llm.structured import structured_complete

    listing = await structured_complete(
        client, prompt, response_model=JobListing
    )
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, TypeVar

import structlog
from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    from core.llm.protocol import LLMClient

logger = structlog.get_logger(__name__)

T = TypeVar("T", bound=BaseModel)

_STRUCTURED_SYSTEM_PROMPT = (
    "Respond ONLY with valid JSON matching the schema provided. "
    "Do not include markdown code fences, preamble, or explanation. "
    "Output raw JSON only."
)


async def structured_complete(
    client: LLMClient,
    prompt: str,
    *,
    response_model: type[T],
    max_retries: int = 2,
    model: str | None = None,
    max_tokens: int = 4096,
    run_id: str = "",
) -> T:
    """Request a structured response from Claude and validate it.

    Appends the Pydantic model's JSON schema to the prompt so Claude
    knows the expected shape.  Retries on parse/validation failure with
    an error-correction prompt.

    Args:
        client: The LLM client instance.
        prompt: The user prompt describing what to extract/generate.
        response_model: A Pydantic model class to validate against.
        max_retries: Number of retry attempts on validation failure.
        model: Optional model override.
        max_tokens: Maximum tokens in the response.
        run_id: Pipeline run identifier for log correlation.

    Returns:
        A validated instance of ``response_model``.

    Raises:
        ValueError: If the response cannot be parsed after all retries.
    """
    schema_json = json.dumps(response_model.model_json_schema(), indent=2)
    full_prompt = f"{prompt}\n\nRespond with JSON matching this schema:\n{schema_json}"

    last_error: str = ""
    for attempt in range(1 + max_retries):
        retry_context = ""
        if attempt > 0:
            retry_context = (
                f"\n\nYour previous response was invalid: {last_error}\n"
                "Please fix the JSON and try again."
            )

        raw = await client.complete(
            full_prompt + retry_context,
            system=_STRUCTURED_SYSTEM_PROMPT,
            model=model,
            max_tokens=max_tokens,
            run_id=run_id,
        )

        # Strip markdown fences if Claude includes them despite instructions.
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned.rsplit("```", 1)[0]
        cleaned = cleaned.strip()

        try:
            data = json.loads(cleaned)
            result = response_model.model_validate(data)
            logger.info(
                "structured_output_success",
                model_type=response_model.__name__,
                attempt=attempt + 1,
            )
            return result
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = str(exc)
            logger.warning(
                "structured_output_retry",
                model_type=response_model.__name__,
                attempt=attempt + 1,
                error=last_error[:200],
            )

    msg = (
        f"Failed to get valid {response_model.__name__} after "
        f"{1 + max_retries} attempts. Last error: {last_error}"
    )
    raise ValueError(msg)
