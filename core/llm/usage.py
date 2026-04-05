"""Cumulative LLM usage and cost tracking."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

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

        self._call_log.append(
            {
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": round(call_cost, 6),
                "latency_ms": round(latency_ms, 1),
            }
        )

        return call_cost
