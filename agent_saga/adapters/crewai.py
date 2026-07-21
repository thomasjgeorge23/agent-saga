"""CrewAI adapter.

Drop a saga boundary around a CrewAI crew, and wrap the tools whose effects
must unwind. A wrapped tool keeps its name, description, and args schema, so the
agent and the crew treat it exactly like the original.

CrewAI tools are typically *synchronous* (`BaseTool._run`), unlike LangChain's
async-first tools. The wrapper bridges that: the saga runs the sync `_run` on a
worker thread (so it never blocks the event loop), while presenting the tool to
CrewAI unchanged.

The routing core lives in `.._common.build_runner` and is tested without CrewAI
installed; CrewAI is imported lazily, only inside `wrap_tool`.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Optional

from ..context import SagaAborted
from ..decorator import saga_scope
from ..semantics import ActionSemantics, CompensationFactory
from ._common import build_runner


def _sync_caller(run_sync: Callable[..., Any]) -> Callable[..., Any]:
    """Adapt a CrewAI tool's synchronous `_run(**kwargs)` into the async `call`
    that build_runner expects, off the event loop."""

    async def _call(**kwargs: Any) -> Any:
        return await asyncio.to_thread(lambda: run_sync(**kwargs))

    return _call


def wrap_tool(
    tool: Any,
    *,
    semantics: ActionSemantics,
    compensate: Optional[CompensationFactory] = None,
    timeout: Optional[float] = None,
    name: Optional[str] = None,
) -> Any:
    """Return a saga-aware drop-in for a CrewAI `BaseTool`.

    The returned object is a CrewAI `BaseTool` subclass instance whose `_run`
    routes through the active saga. Its `name`, `description`, and `args_schema`
    are copied from the original so the crew sees no difference.
    """
    from crewai.tools import BaseTool  # lazy

    if not isinstance(tool, BaseTool):
        raise TypeError(
            f"expected a crewai.tools.BaseTool, got {type(tool).__name__}. Define "
            f"the tool as a BaseTool (or via crewai's @tool) and wrap that."
        )

    tool_name = name or tool.name
    runner = build_runner(
        _sync_caller(tool._run), name=tool_name, semantics=semantics,
        compensate=compensate, timeout=timeout,
    )

    def _run(self, **kwargs: Any) -> Any:
        # CrewAI calls _run synchronously. Usually there is no running loop, so
        # asyncio.run is fine. But if _run is invoked from a thread that already
        # drives a loop, asyncio.run would raise -- so fall back to a dedicated
        # worker thread with its own loop, carrying the current context (and thus
        # the active saga) across with copy_context().
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(runner(**kwargs))

        import concurrent.futures
        import contextvars

        ctx = contextvars.copy_context()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(lambda: ctx.run(asyncio.run, runner(**kwargs))).result()

    wrapped_cls = type(
        f"Saga{type(tool).__name__}",
        (BaseTool,),
        {"_run": _run},
    )
    return wrapped_cls(
        name=tool_name,
        description=tool.description,
        args_schema=tool.args_schema,
    )


async def saga_kickoff(
    crew: Any,
    inputs: Optional[dict] = None,
    *,
    gate: Any = None,
    wal: Any = None,
    halt_on_compensation_failure: bool = True,
    reraise: bool = True,
    kickoff: Optional[Callable[[Any], Any]] = None,
) -> Any:
    """Run a CrewAI crew inside a saga boundary.

    On success returns the crew's output. On any exception, every wrapped tool
    that already executed is compensated LIFO, then `SagaAborted` is raised
    (carrying the `RollbackReport`) unless `reraise=False`.

    CrewAI's `kickoff` is synchronous, so it is driven on a worker thread to keep
    the saga's event loop responsive. `crew.kickoff_async` is used automatically
    when present; override the whole call with `kickoff=<coroutine (ctx)->result>`.
    """
    try:
        async with saga_scope(
            gate=gate, wal=wal,
            halt_on_compensation_failure=halt_on_compensation_failure,
        ) as ctx:
            if kickoff is not None:
                return await kickoff(ctx)
            if hasattr(crew, "kickoff_async"):
                return await crew.kickoff_async(inputs=inputs)
            return await asyncio.to_thread(lambda: crew.kickoff(inputs=inputs))
    except SagaAborted as aborted:
        if reraise:
            raise
        return aborted.report


__all__ = ["wrap_tool", "saga_kickoff"]
