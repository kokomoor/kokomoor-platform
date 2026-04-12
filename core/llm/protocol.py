"""Provider-agnostic LLM client interface."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from core.llm.usage import LLMUsage


@runtime_checkable
class LLMClient(Protocol):
    """Structural contract for any LLM backend used by pipelines.

    Implementations (e.g. Anthropic, OpenAI, Gemini) must expose cumulative
    ``usage`` and an async ``complete`` method with a shared keyword-only API.
    """

    usage: LLMUsage

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
        """Send a completion request and return the assistant text.

        Args:
            prompt: The user message content.
            system: Optional system prompt.
            max_tokens: Maximum tokens in the response.
            temperature: Sampling temperature (0.0 = deterministic).
            model: Override the default model for this call.
            run_id: Pipeline run identifier for log correlation.
            cache_system: If True, mark the ``system`` prompt as a
                cacheable prefix so it can be reused across calls.
        """
        ...
