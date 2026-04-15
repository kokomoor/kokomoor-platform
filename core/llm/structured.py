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
    from collections.abc import Callable

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
    system_prefix: str | None = None,
    cache_system: bool = False,
    validator: Callable[[T], None] | None = None,
) -> T:
    """Request a structured response from Claude and validate it.

    Appends the Pydantic model's JSON schema to the prompt so Claude
    knows the expected shape.  Retries on parse/validation/semantic
    failure with an error-correction prompt.

    Args:
        client: The LLM client instance.
        prompt: The user prompt describing what to extract/generate.
        response_model: A Pydantic model class to validate against.
        max_retries: Number of retry attempts on validation failure.
        model: Optional model override.
        max_tokens: Maximum tokens in the response.
        run_id: Pipeline run identifier for log correlation.
        system_prefix: Optional stable text to prepend to the system
            message (e.g. a large style guide or prompt preamble).
            Keeping this byte-identical across calls is required for the
            prompt cache to hit.
        cache_system: If True, mark the (composed) system prompt as a
            cacheable prefix via ``cache_control={"type":"ephemeral"}``.
            Only meaningful when ``system_prefix`` is large enough to
            exceed the per-model cache minimum (~1024 tokens).
        validator: Optional semantic validator invoked after Pydantic
            validation succeeds. When it raises ``ValueError`` (or any
            exception), the retry loop feeds the error message back to
            the model as correction context, identical to the path used
            for JSON/Pydantic errors. Use this for domain rules that
            cannot be expressed in the schema (word budgets, banned
            phrases, cross-field consistency). The validator may mutate
            ``result`` for normalization but must not perform I/O.

    Returns:
        A validated instance of ``response_model``.

    Raises:
        ValueError: If the response cannot be parsed after all retries.
    """
    schema_json = json.dumps(response_model.model_json_schema(), indent=2)

    # When the caller asks for prompt caching, fold the schema into the
    # system message instead of the user prompt. The schema is identical
    # across every call in a run, so it belongs in the cacheable prefix.
    # This also pushes the prefix above the per-model minimum cache size
    # (1024 tokens for Sonnet/Opus, 2048 for Haiku) when the caller's
    # own system_prefix is too small to qualify on its own. When caching
    # is off we keep the legacy behaviour of appending the schema to the
    # user prompt so non-cached callers behave identically.
    if cache_system:
        system_parts = []
        if system_prefix:
            system_parts.append(system_prefix)
        system_parts.append("Respond with JSON matching this schema:\n" + schema_json)
        system_parts.append(_STRUCTURED_SYSTEM_PROMPT)
        system_message = "\n\n".join(system_parts)
        full_prompt = prompt
    else:
        full_prompt = f"{prompt}\n\nRespond with JSON matching this schema:\n{schema_json}"
        if system_prefix:
            system_message = f"{system_prefix}\n\n{_STRUCTURED_SYSTEM_PROMPT}"
        else:
            system_message = _STRUCTURED_SYSTEM_PROMPT

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
            system=system_message,
            model=model,
            max_tokens=max_tokens,
            run_id=run_id,
            cache_system=cache_system,
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
            if validator is not None:
                validator(result)
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
        except ValueError as exc:
            # Raised by the semantic ``validator``. Treat it like a
            # Pydantic ValidationError: feed the message back to the
            # model so it can fix the specific rule it violated.
            last_error = str(exc)
            logger.warning(
                "structured_output_semantic_retry",
                model_type=response_model.__name__,
                attempt=attempt + 1,
                error=last_error[:200],
            )

    msg = (
        f"Failed to get valid {response_model.__name__} after "
        f"{1 + max_retries} attempts. Last error: {last_error}"
    )
    raise ValueError(msg)
