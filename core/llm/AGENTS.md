# AGENTS.md — core/llm/

Provider-agnostic LLM abstraction layer. This is the most architecturally significant subsystem in `core/`.

## File map

| File | Role |
|------|------|
| `protocol.py` | `LLMClient` — the `typing.Protocol` that all providers implement |
| `anthropic.py` | `AnthropicClient` — production Anthropic implementation |
| `usage.py` | `LLMUsage` — cumulative token/cost/cache tracking dataclass |
| `structured.py` | `structured_complete()` — JSON output with Pydantic validation + retries |
| `__init__.py` | Re-exports: `LLMClient`, `AnthropicClient`, `LLMUsage` |

## How to add a new LLM provider

1. Create `core/llm/<provider>.py` (e.g. `openai.py`)
2. Implement a class that satisfies the `LLMClient` protocol:
   - `usage: LLMUsage` attribute
   - `async def complete(self, prompt, *, system, max_tokens, temperature, model, run_id) -> str`
3. Add re-export in `core/llm/__init__.py` and `__all__`
4. Verify: `isinstance(YourClient(), LLMClient)` must be `True`

## Invariants

- **Pipeline code depends on `LLMClient` only.** Never import `anthropic`, `openai`, etc. in pipeline modules.
- **`MockLLMClient` in `core/testing/` must always satisfy the protocol.** If you change `protocol.py`, update `MockLLMClient` and `AnthropicClient` in the same change.
- **`structured.py` wraps `client.complete()`.** It passes through `run_id` and `model`. Don't bypass it for structured outputs.

## Logging contract

| Level | What is logged |
|-------|---------------|
| **INFO** | `llm_request_start` / `llm_request_complete` — model, request_id, run_id, tokens, cost, latency, stop_reason, cache status |
| **DEBUG** | `llm_request_prompt` / `llm_request_response` — full prompt text and full response text |
| **WARNING** | `llm_response_truncated` — when `stop_reason == "max_tokens"` (response was cut off) |
| **WARNING** | `llm_retry` — before retry on rate limit or connection error |
| **ERROR** | `llm_request_failed` — exception with traceback |

DEBUG-level prompt/response logging is gated so production (INFO+) never sees full content.

## Pricing tables

`usage.py` contains per-model pricing in `_COST_PER_1M_INPUT` / `_COST_PER_1M_OUTPUT`. Update these when adding new models or when Anthropic changes pricing. Unknown models fall back to Sonnet-tier pricing.
