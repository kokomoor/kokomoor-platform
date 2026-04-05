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
    ) -> str:
        """Send a completion request and return the assistant text."""
        ...
