"""Anthropic Claude API client with production-grade resilience."""

from __future__ import annotations

import time
import uuid
from typing import Any

import anthropic
import structlog
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from core.config import get_settings
from core.llm.usage import LLMUsage

logger = structlog.get_logger(__name__)


def _log_llm_retry(retry_state: RetryCallState) -> None:
    wait_s = 0.0
    if retry_state.next_action is not None:
        wait_s = retry_state.next_action.sleep
    structlog.get_logger().warning(
        "llm_retry",
        attempt=retry_state.attempt_number,
        wait=wait_s,
    )


class AnthropicClient:
    """Anthropic Messages API client with retries and usage tracking.

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
        before_sleep=_log_llm_retry,
    )
    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        model: str | None = None,
        run_id: str = "",
        cache_system: bool = False,
    ) -> str:
        """Send a completion request and return the text response.

        Args:
            prompt: The user message content.
            system: Optional system prompt.
            max_tokens: Maximum tokens in the response.
            temperature: Sampling temperature (0.0 = deterministic).
            model: Override the default model for this call.
            run_id: Pipeline run identifier for log correlation.
            cache_system: If True and ``system`` is set, mark the system
                prompt with ``cache_control={"type":"ephemeral"}`` so the
                Anthropic prefix cache can reuse it across calls. The
                system text must be stable byte-for-byte and long enough
                to hit the per-model minimum (~1024 tokens) to be cached.

        Returns:
            The assistant's text response.

        Raises:
            anthropic.APIError: If the request fails after retries.
        """
        model = model or self._model
        request_id = uuid.uuid4().hex[:16]
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]

        system_param: Any
        if system is None:
            system_param = anthropic.NOT_GIVEN
        elif cache_system:
            system_param = [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        else:
            system_param = system

        log = logger.bind(
            model=model,
            request_id=request_id,
            run_id=run_id or None,
            temperature=temperature,
            max_tokens=max_tokens,
            prompt_len=len(prompt),
            cache_system=cache_system,
        )
        log.info("llm_request_start")
        log.debug("llm_request_prompt", prompt=prompt, system=system)

        start = time.monotonic()
        try:
            response = await self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system_param,
                messages=messages,  # type: ignore[arg-type]
            )
        except Exception:
            self.usage.errors += 1
            log.exception("llm_request_failed")
            raise

        latency_ms = (time.monotonic() - start) * 1000
        stop_reason = response.stop_reason or "unknown"

        cache_hit: bool | None = None
        cache_creation_tokens = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
        cache_read_tokens = getattr(response.usage, "cache_read_input_tokens", 0) or 0
        if cache_read_tokens > 0:
            cache_hit = True
        elif cache_creation_tokens > 0:
            cache_hit = False

        cost = self.usage.record(
            model=model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            latency_ms=latency_ms,
            request_id=request_id,
            stop_reason=stop_reason,
            temperature=temperature,
            max_tokens=max_tokens,
            cache_hit=cache_hit,
        )

        text_blocks = [block.text for block in response.content if block.type == "text"]
        response_text = "\n".join(text_blocks)

        log.info(
            "llm_request_complete",
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            latency_ms=round(latency_ms, 1),
            cost_usd=round(cost, 6),
            stop_reason=stop_reason,
            cache_hit=cache_hit,
            cache_creation_tokens=cache_creation_tokens,
            cache_read_tokens=cache_read_tokens,
            response_len=len(response_text),
        )
        log.debug("llm_request_response", response_text=response_text)

        if stop_reason == "max_tokens":
            log.warning(
                "llm_response_truncated",
                output_tokens=response.usage.output_tokens,
                max_tokens=max_tokens,
            )

        return response_text
