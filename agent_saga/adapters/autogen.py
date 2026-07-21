"""AutoGen adapter.

AutoGen has moved through several tool APIs (pyautogen's `register_function`,
autogen-core's `FunctionTool`, AG2), but they share one root: a tool is a plain
Python callable whose name and docstring become the schema the model sees. So
this adapter wraps at that stable layer -- a callable in, a saga-aware callable
out -- which works across those variants without pinning to one.

    saga_charge = wrap_tool(charge, semantics=COMPENSABLE, compensate=...)
    # register saga_charge with your agent exactly as you would `charge`

The returned callable keeps `__name__`, `__doc__`, and signature metadata, and
routes through the active saga (passing through untouched outside one). The
routing core is the shared `build_runner`.
"""

from __future__ import annotations

import asyncio
import functools
from typing import Any, Callable, Optional

from ..context import SagaAborted
from ..decorator import saga_scope
from ..semantics import ActionSemantics, CompensationFactory
from ._common import build_runner


def wrap_tool(
    fn: Callable[..., Any],
    *,
    semantics: ActionSemantics,
    compensate: Optional[CompensationFactory] = None,
    timeout: Optional[float] = None,
    name: Optional[str] = None,
) -> Callable[..., Any]:
    """Wrap a tool callable so its execution records on the active saga.

    `fn` may be sync or async. The wrapper is always async (AutoGen awaits async
    tools), preserves the original name/docstring/signature so AutoGen builds the
    same schema, and derives the compensation from the call's result at runtime.
    """
    tool_name = name or getattr(fn, "__name__", "tool")

    if asyncio.iscoroutinefunction(fn):
        async def _call(**kwargs: Any) -> Any:
            return await fn(**kwargs)
    else:
        async def _call(**kwargs: Any) -> Any:
            return await asyncio.to_thread(lambda: fn(**kwargs))

    runner = build_runner(
        _call, name=tool_name, semantics=semantics,
        compensate=compensate, timeout=timeout,
    )

    @functools.wraps(fn)
    async def wrapper(**kwargs: Any) -> Any:
        return await runner(**kwargs)

    # functools.wraps copies __wrapped__/__doc__/__name__; make sure the name
    # override (if any) wins for schema generation.
    wrapper.__name__ = tool_name
    return wrapper


async def saga_run(
    run: Callable[[Any], Any],
    *,
    gate: Any = None,
    wal: Any = None,
    halt_on_compensation_failure: bool = True,
    reraise: bool = True,
) -> Any:
    """Run an AutoGen conversation inside a saga boundary.

    AutoGen's entry points vary (`a_initiate_chat`, `run`, a team's `run`), so
    this takes a coroutine `run(saga_context) -> result` rather than guessing.
    On any exception, wrapped tools that executed are compensated LIFO and
    `SagaAborted` is raised unless `reraise=False`.

        await saga_run(lambda saga: agent.a_initiate_chat(other, message="..."))
    """
    try:
        async with saga_scope(
            gate=gate, wal=wal,
            halt_on_compensation_failure=halt_on_compensation_failure,
        ) as ctx:
            return await run(ctx)
    except SagaAborted as aborted:
        if reraise:
            raise
        return aborted.report


__all__ = ["wrap_tool", "saga_run"]
