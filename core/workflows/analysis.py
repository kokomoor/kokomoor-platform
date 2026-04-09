"""Reusable structured-analysis workflow engine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, TypeVar

import structlog
from pydantic import BaseModel

from core.llm.structured import structured_complete

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from core.llm.protocol import LLMClient

logger = structlog.get_logger(__name__)

StateT = TypeVar("StateT")
ItemT = TypeVar("ItemT")
ResponseT = TypeVar("ResponseT", bound=BaseModel)
RuntimeT = TypeVar("RuntimeT")


@dataclass(frozen=True)
class StructuredAnalysisSpec(Generic[StateT, ItemT, ResponseT, RuntimeT]):
    """Spec-driven hooks for running structured analysis over state items."""

    name: str
    response_model: type[ResponseT]
    prepare: Callable[[StateT], RuntimeT]
    get_items: Callable[[StateT], list[ItemT]]
    should_skip: Callable[[StateT], bool]
    on_skip: Callable[[StateT], None]
    build_prompt: Callable[[StateT, ItemT, RuntimeT], str]
    get_run_id: Callable[[StateT], str]
    get_model: Callable[[StateT, RuntimeT], str | None]
    get_max_tokens: Callable[[StateT, RuntimeT], int]
    get_cache_key: Callable[[StateT, ItemT, RuntimeT], str | None]
    get_cached_result: Callable[[StateT, str, RuntimeT], ResponseT | None]
    cache_result: Callable[[StateT, str, ResponseT, RuntimeT], None]
    on_item_start: Callable[[StateT, ItemT, RuntimeT], None]
    on_item_result: Callable[[StateT, ItemT, ResponseT, RuntimeT], None]
    on_item_error: Callable[[StateT, ItemT, Exception, RuntimeT], None]
    on_complete: Callable[[StateT, RuntimeT], None]
    write_inspection_artifacts: (
        Callable[[StateT, ItemT, str, ResponseT, RuntimeT], Awaitable[None]] | None
    ) = None


class StructuredAnalysisEngine(Generic[StateT, ItemT, ResponseT, RuntimeT]):
    """Reusable engine for LLM-backed structured analysis passes."""

    async def run(
        self,
        state: StateT,
        *,
        llm_client: LLMClient,
        spec: StructuredAnalysisSpec[StateT, ItemT, ResponseT, RuntimeT],
    ) -> StateT:
        if spec.should_skip(state):
            logger.info("structured_analysis.skip", workflow=spec.name)
            spec.on_skip(state)
            return state

        runtime = spec.prepare(state)
        items = spec.get_items(state)
        for item in items:
            cache_key = spec.get_cache_key(state, item, runtime)
            if cache_key is not None:
                cached = spec.get_cached_result(state, cache_key, runtime)
                if cached is not None:
                    logger.info(
                        "structured_analysis.cache_hit", workflow=spec.name, cache_key=cache_key
                    )
                    spec.on_item_result(state, item, cached, runtime)
                    continue

            try:
                spec.on_item_start(state, item, runtime)
                prompt = spec.build_prompt(state, item, runtime)
                result = await structured_complete(
                    llm_client,
                    prompt,
                    response_model=spec.response_model,
                    model=spec.get_model(state, runtime),
                    max_tokens=spec.get_max_tokens(state, runtime),
                    run_id=spec.get_run_id(state),
                )
                if spec.write_inspection_artifacts is not None:
                    await spec.write_inspection_artifacts(state, item, prompt, result, runtime)
                spec.on_item_result(state, item, result, runtime)
                if cache_key is not None:
                    spec.cache_result(state, cache_key, result, runtime)
            except Exception as exc:
                spec.on_item_error(state, item, exc, runtime)

        spec.on_complete(state, runtime)
        return state
