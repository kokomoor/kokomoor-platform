"""Tests for generic structured analysis workflow engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel

from core.testing import MockLLMClient
from core.workflows.analysis import StructuredAnalysisEngine, StructuredAnalysisSpec

if TYPE_CHECKING:
    from collections.abc import Callable


class AnalysisOut(BaseModel):
    value: str


@dataclass
class DummyState:
    items: list[str] = field(default_factory=list)
    results: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    cache: dict[str, AnalysisOut] = field(default_factory=dict)
    skipped: bool = False
    run_id: str = "run-123"
    dry_run: bool = False


@dataclass(frozen=True)
class Runtime:
    model: str | None
    max_tokens: int


def _spec(
    *,
    prepare: Callable[[DummyState], Runtime] | None = None,
    on_item_start: Callable[[DummyState, str, Runtime], None] | None = None,
) -> StructuredAnalysisSpec[DummyState, str, AnalysisOut, Runtime]:
    item_start_hook = on_item_start or (lambda _state, _item, _runtime: None)
    prepare_hook = prepare or (lambda _state: Runtime(model="model-x", max_tokens=99))
    return StructuredAnalysisSpec(
        name="dummy_analysis",
        response_model=AnalysisOut,
        prepare=prepare_hook,
        get_items=lambda state: state.items,
        should_skip=lambda state: state.dry_run,
        on_skip=lambda state: setattr(state, "skipped", True),
        build_prompt=lambda _state, item, _runtime: f"analyze::{item}",
        get_run_id=lambda state: state.run_id,
        get_model=lambda _state, runtime: runtime.model,
        get_max_tokens=lambda _state, runtime: runtime.max_tokens,
        get_cache_key=lambda _state, item, _runtime: item,
        get_cached_result=lambda state, key, _runtime: state.cache.get(key),
        cache_result=lambda state, key, result, _runtime: state.cache.__setitem__(key, result),
        on_item_start=item_start_hook,
        on_item_result=lambda state, item, result, _runtime: state.results.__setitem__(
            item, result.value
        ),
        on_item_error=lambda state, item, exc, _runtime: state.errors.append(f"{item}:{exc}"),
        on_complete=lambda _state, _runtime: None,
    )


@pytest.mark.asyncio
async def test_skip_behavior() -> None:
    state = DummyState(items=["a"], dry_run=True)
    engine: StructuredAnalysisEngine[DummyState, str, AnalysisOut, Runtime] = (
        StructuredAnalysisEngine()
    )

    result = await engine.run(state, llm_client=MockLLMClient(responses=[]), spec=_spec())

    assert result.skipped is True
    assert result.results == {}


@pytest.mark.asyncio
async def test_skip_does_not_prepare_runtime() -> None:
    state = DummyState(items=["a"], dry_run=True)
    prepare_calls = 0

    engine: StructuredAnalysisEngine[DummyState, str, AnalysisOut, Runtime] = (
        StructuredAnalysisEngine()
    )

    def _count_prepare() -> Runtime:
        nonlocal prepare_calls
        prepare_calls += 1
        return Runtime(model="model-x", max_tokens=99)

    await engine.run(
        state,
        llm_client=MockLLMClient(responses=[]),
        spec=_spec(prepare=lambda _state: _count_prepare()),
    )

    assert prepare_calls == 0


@pytest.mark.asyncio
async def test_cache_hit_and_cache_miss() -> None:
    cached = AnalysisOut(value="from-cache")
    state = DummyState(items=["hit", "miss"], cache={"hit": cached})
    engine: StructuredAnalysisEngine[DummyState, str, AnalysisOut, Runtime] = (
        StructuredAnalysisEngine()
    )
    client = MockLLMClient(responses=['{"value":"from-llm"}'])

    await engine.run(state, llm_client=client, spec=_spec())

    assert state.results["hit"] == "from-cache"
    assert state.results["miss"] == "from-llm"
    assert len(client.calls) == 1
    assert state.cache["miss"].value == "from-llm"


@pytest.mark.asyncio
async def test_error_propagation() -> None:
    state = DummyState(items=["bad"])
    engine: StructuredAnalysisEngine[DummyState, str, AnalysisOut, Runtime] = (
        StructuredAnalysisEngine()
    )
    spec = _spec(
        on_item_start=lambda _state, _item, _runtime: (_ for _ in ()).throw(ValueError("boom"))
    )

    await engine.run(state, llm_client=MockLLMClient(responses=[]), spec=spec)

    assert state.errors and "boom" in state.errors[0]


@pytest.mark.asyncio
async def test_prompt_and_model_token_overrides() -> None:
    state = DummyState(items=["x"])
    engine: StructuredAnalysisEngine[DummyState, str, AnalysisOut, Runtime] = (
        StructuredAnalysisEngine()
    )
    client = MockLLMClient(responses=['{"value":"ok"}'])

    await engine.run(state, llm_client=client, spec=_spec())

    prompt, kwargs = client.calls[0]
    assert prompt == "analyze::x"
    assert kwargs["model"] == "model-x"
    assert kwargs["max_tokens"] == 99
    assert kwargs["run_id"] == "run-123"


@pytest.mark.asyncio
async def test_state_writeback() -> None:
    state = DummyState(items=["a", "b"])
    engine: StructuredAnalysisEngine[DummyState, str, AnalysisOut, Runtime] = (
        StructuredAnalysisEngine()
    )
    client = MockLLMClient(responses=['{"value":"A"}', '{"value":"B"}'])

    await engine.run(state, llm_client=client, spec=_spec())

    assert state.results == {"a": "A", "b": "B"}
