"""Provider-agnostic LLM abstractions and implementations.

Pipelines and helpers depend on :class:`LLMClient` (a :class:`typing.Protocol`)
and :class:`LLMUsage` only. Swap backends by passing a different concrete
client that implements the same interface (e.g. :class:`AnthropicClient`).

Usage:
    from core.llm import AnthropicClient, LLMClient

    client: LLMClient = AnthropicClient()
    response = await client.complete("Summarise this job listing.")
"""

from __future__ import annotations

from core.llm.anthropic import AnthropicClient
from core.llm.protocol import LLMClient
from core.llm.usage import LLMUsage

__all__ = ["AnthropicClient", "LLMClient", "LLMUsage"]
