"""Reusable structured-analysis workflow engine."""

from __future__ import annotations

import asyncio
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
    # Optional cacheable system prefix. Must return the SAME bytes on
    # every call within a run so the Anthropic prefix cache hits.
    build_cached_system: Callable[[StateT, RuntimeT], str | None] | None = None
    # Upper bound on concurrent in-flight LLM requests. 1 = sequential.
    concurrency: int = 1


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
        cached_system = (
            spec.build_cached_system(state, runtime)
            if spec.build_cached_system is not None
            else None
        )

        # Pass 1: resolve cache hits synchronously and group remaining
        # items by cache_key. Grouping preserves a subtle guarantee
        # from the old sequential loop: two items with the same cache
        # key triggered only one LLM call, because item 2 found the
        # write from item 1. In parallel mode we get that by running
        # one representative per cache_key and fanning the result out
        # to every item in the group.
        unkeyed: list[ItemT] = []
        grouped: dict[str, list[ItemT]] = {}
        for item in items:
            cache_key = spec.get_cache_key(state, item, runtime)
            if cache_key is not None:
                cached = spec.get_cached_result(state, cache_key, runtime)
                if cached is not None:
                    logger.info(
                        "structured_analysis.cache_hit",
                        workflow=spec.name,
                        cache_key=cache_key,
                    )
                    spec.on_item_result(state, item, cached, runtime)
                    continue
                grouped.setdefault(cache_key, []).append(item)
            else:
                unkeyed.append(item)

        if grouped or unkeyed:
            concurrency = max(1, spec.concurrency)
            semaphore = asyncio.Semaphore(concurrency)

            async def _process_group(cache_key: str | None, group: list[ItemT]) -> None:
                # Representative item drives the actual LLM call; the
                # result is then applied to every item in the group.
                representative = group[0]
                async with semaphore:
                    try:
                        spec.on_item_start(state, representative, runtime)
                        prompt = spec.build_prompt(state, representative, runtime)
                        result = await structured_complete(
                            llm_client,
                            prompt,
                            response_model=spec.response_model,
                            model=spec.get_model(state, runtime),
                            max_tokens=spec.get_max_tokens(state, runtime),
                            run_id=spec.get_run_id(state),
                            system_prefix=cached_system,
                            cache_system=cached_system is not None,
                        )
                        if spec.write_inspection_artifacts is not None:
                            await spec.write_inspection_artifacts(
                                state, representative, prompt, result, runtime
                            )
                        spec.on_item_result(state, representative, result, runtime)
                        if cache_key is not None:
                            spec.cache_result(state, cache_key, result, runtime)
                        for follower in group[1:]:
                            spec.on_item_result(state, follower, result, runtime)
                    except Exception as exc:
                        for item in group:
                            spec.on_item_error(state, item, exc, runtime)

            tasks = [_process_group(key, members) for key, members in grouped.items()]
            tasks.extend(_process_group(None, [item]) for item in unkeyed)
            await asyncio.gather(*tasks)

        spec.on_complete(state, runtime)
        return state
