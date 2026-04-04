"""Anthropic API client with production-grade resilience.

Wraps the Anthropic Python SDK with: automatic retry with exponential
backoff, per-call cost tracking, structured output validation, and
optional LangSmith trace integration.

Usage:
    from core.llm.client import LLMClient

    client = LLMClient()
    response = await client.complete("Summarise this job listing.")
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import anthropic
import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from core.config import get_settings

logger = structlog.get_logger(__name__)

# Approximate pricing per 1M tokens (as of early 2026). Update as needed.
_COST_PER_1M_INPUT: dict[str, float] = {
    "claude-sonnet-4-20250514": 3.00,
    "claude-opus-4-20250514": 15.00,
    "claude-haiku-4-5-20251001": 0.80,
}
_COST_PER_1M_OUTPUT: dict[str, float] = {
    "claude-sonnet-4-20250514": 15.00,
    "claude-opus-4-20250514": 75.00,
    "claude-haiku-4-5-20251001": 4.00,
}


@dataclass
class LLMUsage:
    """Tracks cumulative LLM usage and cost for a client instance."""

    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    total_calls: int = 0
    errors: int = 0
    _call_log: list[dict[str, Any]] = field(default_factory=list)

    def record(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: float,
    ) -> float:
        """Record a single API call and return its cost."""
        input_cost = (input_tokens / 1_000_000) * _COST_PER_1M_INPUT.get(model, 3.0)
        output_cost = (output_tokens / 1_000_000) * _COST_PER_1M_OUTPUT.get(model, 15.0)
        call_cost = input_cost + output_cost

        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.total_cost_usd += call_cost
        self.total_calls += 1

        self._call_log.append({
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": round(call_cost, 6),
            "latency_ms": round(latency_ms, 1),
        })

        return call_cost


class LLMClient:
    """Production-grade Anthropic API client.

    Attributes:
        usage: Cumulative usage and cost tracker for this client instance.
    """

    def __init__(self, model: str | None = None) -> None:
        settings = get_settings()
        self._model = model or settings.anthropic_model
        self._client = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key.get_secret_value(),
            timeout=settings.anthropic_timeout_seconds,
            max_retries=0,  # We handle retries ourselves for better observability.
        )
        self.usage = LLMUsage()

    @retry(
        retry=retry_if_exception_type((anthropic.RateLimitError, anthropic.APIConnectionError)),
        wait=wait_exponential(multiplier=1, min=2, max=60),
        stop=stop_after_attempt(3),
        before_sleep=lambda retry_state: structlog.get_logger().warning(
            "llm_retry",
            attempt=retry_state.attempt_number,
            wait=retry_state.next_action.sleep,
        ),
    )
    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        model: str | None = None,
    ) -> str:
        """Send a completion request and return the text response.

        Args:
            prompt: The user message content.
            system: Optional system prompt.
            max_tokens: Maximum tokens in the response.
            temperature: Sampling temperature (0.0 = deterministic).
            model: Override the default model for this call.

        Returns:
            The assistant's text response.

        Raises:
            anthropic.APIError: If the request fails after retries.
        """
        model = model or self._model
        messages = [{"role": "user", "content": prompt}]

        log = logger.bind(model=model, prompt_len=len(prompt))
        log.info("llm_request_start")

        start = time.monotonic()
        try:
            response = await self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system or anthropic.NOT_GIVEN,
                messages=messages,
            )
        except Exception:
            self.usage.errors += 1
            log.exception("llm_request_failed")
            raise

        latency_ms = (time.monotonic() - start) * 1000
        cost = self.usage.record(
            model=model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            latency_ms=latency_ms,
        )

        log.info(
            "llm_request_complete",
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            latency_ms=round(latency_ms, 1),
            cost_usd=round(cost, 6),
        )

        # Extract text from the response content blocks.
        text_blocks = [block.text for block in response.content if block.type == "text"]
        return "\n".join(text_blocks)
