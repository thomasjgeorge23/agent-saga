"""LlamaIndex adapter.

Wrap a LlamaIndex `FunctionTool` so its execution records on the active saga,
and run a LlamaIndex agent inside a saga boundary. The wrapped tool keeps the
original name and description, so the agent and the LLM see no difference.

LlamaIndex tools expose the underlying callable as `.fn` / `.async_fn` and their
metadata as `.metadata.name` / `.metadata.description`. We route the callable
through the saga and rebuild a `FunctionTool` around it. The framework is
imported lazily, only inside `wrap_tool`; the routing core is the shared
`build_runner`, tested without LlamaIndex installed.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Optional

from ..context import SagaAborted
from ..decorator import saga_scope
from ..semantics import ActionSemantics, CompensationFactory
from ._common import build_runner


def _async_callable(tool: Any) -> Callable[..., Any]:
    """An async `call(**kwargs)` over the tool's underlying function, preferring
    the native async implementation and dropping a sync one onto a worker
    thread so it never blocks the loop."""
    async_fn = getattr(tool, "async_fn", None)
    sync_fn = getattr(tool, "fn", None)

    if async_fn is not None:
        async def _call(**kwargs: Any) -> Any:
            return await async_fn(**kwargs)
        return _call
    if sync_fn is not None:
        async def _call(**kwargs: Any) -> Any:
            return await asyncio.to_thread(lambda: sync_fn(**kwargs))
        return _call
    raise TypeError("tool exposes neither .async_fn nor .fn; is it a FunctionTool?")


def wrap_tool(
    tool: Any,
    *,
    semantics: ActionSemantics,
    compensate: Optional[CompensationFactory] = None,
    timeout: Optional[float] = None,
    name: Optional[str] = None,
) -> Any:
    """Return a saga-aware `FunctionTool` drop-in.

    `tool` is a LlamaIndex `FunctionTool`. The result is a new `FunctionTool`
    with the same name and description whose call routes through the active saga
    (and passes through untouched outside one).
    """
    from llama_index.core.tools import FunctionTool  # lazy

    meta = getattr(tool, "metadata", None)
    tool_name = name or (getattr(meta, "name", None) or getattr(tool, "name", "tool"))
    description = getattr(meta, "description", "") or ""

    runner = build_runner(
        _async_callable(tool), name=tool_name, semantics=semantics,
        compensate=compensate, timeout=timeout,
    )

    return FunctionTool.from_defaults(
        async_fn=runner, name=tool_name, description=description,
    )


async def saga_run(
    agent: Any,
    message: Any = None,
    *,
    gate: Any = None,
    wal: Any = None,
    halt_on_compensation_failure: bool = True,
    reraise: bool = True,
    run: Optional[Callable[[Any], Any]] = None,
) -> Any:
    """Run a LlamaIndex agent inside a saga boundary.

    By default drives `agent.achat(message)`. Override with `run=<coroutine
    (saga_context) -> result>` for a workflow, `AgentRunner`, or streaming. On
    any exception every wrapped tool that executed is compensated LIFO, then
    `SagaAborted` is raised unless `reraise=False`.
    """
    try:
        async with saga_scope(
            gate=gate, wal=wal,
            halt_on_compensation_failure=halt_on_compensation_failure,
        ) as ctx:
            if run is not None:
                return await run(ctx)
            return await agent.achat(message)
    except SagaAborted as aborted:
        if reraise:
            raise
        return aborted.report


__all__ = ["wrap_tool", "saga_run"]
