"""LangGraph / LangChain adapter.

The promise: drop this into an existing LangGraph agent and its tool calls
become transactional, without rewriting the graph.

Two entry points:

  * `wrap_tool(tool, semantics=..., compensate=...)` -- takes a LangChain tool
    and returns a drop-in replacement whose execution is recorded on the active
    saga, with a runtime-derived compensation. Same name, description, and args
    schema, so the model and the graph see no difference.

  * `saga_run(graph, input)` -- runs a compiled graph inside a saga boundary.
    If the graph raises, every tool that already ran is compensated LIFO.

The routing logic (`build_runner`) is deliberately separable from the LangChain
packaging so it can be tested without LangChain installed -- the framework is
imported lazily, only inside `wrap_tool`.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from ..decorator import current_saga, saga_scope
from ..context import SagaAborted
from ..semantics import ActionSemantics, CompensationFactory

# A LangChain tool's `ainvoke` takes a single input dict and returns the result.
ToolAInvoke = Callable[[dict], Awaitable[Any]]


def build_runner(
    ainvoke: ToolAInvoke,
    *,
    name: str,
    semantics: ActionSemantics,
    compensate: Optional[CompensationFactory] = None,
    timeout: Optional[float] = None,
) -> Callable[..., Awaitable[Any]]:
    """Wrap a tool's `ainvoke` so it records on the active saga.

    The returned coroutine takes keyword arguments (LangChain calls a structured
    tool's function with the schema fields as kwargs) and:

      * outside a saga, calls the tool untouched -- so the same wrapped tool is
        usable in a plain script or a unit test with no ceremony;
      * inside a saga, routes through `SagaContext.execute`, passing the tool's
        arguments as `policy_args` so pre-flight rules can actually see them
        (an argument hidden in a closure is invisible to the gate -- the exact
        bug that let a connector bypass a threshold rule).
    """

    async def _run(**kwargs: Any) -> Any:
        # The tool's forward call. An async def (not a lambda returning a
        # coroutine) so SagaContext._invoke recognizes it as awaitable rather
        # than shipping it to a worker thread.
        async def _forward() -> Any:
            return await ainvoke(dict(kwargs))

        ctx = current_saga()
        if ctx is None:
            return await _forward()

        return await ctx.execute(
            tool=name,
            semantics=semantics,
            forward=_forward,
            compensate=compensate,
            policy_args=dict(kwargs),
            timeout=timeout,
        )

    _run.__name__ = f"saga_{name}".replace(".", "_")
    return _run


def wrap_tool(
    tool: Any,
    *,
    semantics: ActionSemantics,
    compensate: Optional[CompensationFactory] = None,
    timeout: Optional[float] = None,
    name: Optional[str] = None,
) -> Any:
    """Return a saga-aware drop-in for a LangChain tool.

    `tool` may be a `BaseTool` or a plain function (which is promoted to a
    `StructuredTool` first). The result is a `StructuredTool` with the original
    name, description, and args schema, so `model.bind_tools([...])` and
    `ToolNode` treat it exactly like the original.

    `compensate` is the runtime factory `(tool_result) -> Compensation | None`.
    For an `IRREVERSIBLE` tool it may be omitted -- the gate stops it before it
    runs, so no inverse is needed.
    """
    from langchain_core.tools import BaseTool, StructuredTool

    if not isinstance(tool, BaseTool):
        if not (getattr(tool, "__doc__", None) or "").strip():
            raise ValueError(
                f"{getattr(tool, '__name__', tool)!r} needs a docstring (or pass a "
                f"BaseTool): LangChain uses it as the tool description the model "
                f"sees. This is LangChain's requirement, surfaced early."
            )
        tool = StructuredTool.from_function(tool)

    tool_name = name or tool.name
    runner = build_runner(
        tool.ainvoke, name=tool_name, semantics=semantics,
        compensate=compensate, timeout=timeout,
    )

    if tool.args_schema is not None:
        # Reuse the original schema verbatim; do not let StructuredTool try to
        # infer one from the runner's **kwargs signature.
        return StructuredTool.from_function(
            coroutine=runner,
            name=tool_name,
            description=tool.description,
            args_schema=tool.args_schema,
            infer_schema=False,
        )
    return StructuredTool.from_function(
        coroutine=runner, name=tool_name, description=tool.description,
    )


async def saga_run(
    graph: Any,
    input: Any = None,
    *,
    config: Any = None,
    gate: Any = None,
    wal: Any = None,
    halt_on_compensation_failure: bool = True,
    reraise: bool = True,
    invoke: Optional[Callable[[Any], Awaitable[Any]]] = None,
) -> Any:
    """Run a compiled LangGraph graph inside a saga boundary.

    On success returns the graph's output. On any exception, every wrapped tool
    that already executed is compensated LIFO; then, if `reraise` (default),
    `SagaAborted` is raised carrying the `RollbackReport`, otherwise the report
    is returned.

    `invoke` overrides how the graph is driven -- pass a coroutine
    `(saga_context) -> result` for `astream`, custom configs, or to interleave
    your own steps. When omitted, `graph.ainvoke(input, config)` is used.

    Note on propagation: the active saga is stored in a contextvar. LangGraph
    runs tools within the same context (async tasks copy it; sync tools run via
    a context-preserving executor), so wrapped tools see the saga without any
    threading of arguments through graph state.
    """
    try:
        async with saga_scope(
            gate=gate, wal=wal,
            halt_on_compensation_failure=halt_on_compensation_failure,
        ) as ctx:
            if invoke is not None:
                return await invoke(ctx)
            if config is not None:
                return await graph.ainvoke(input, config)
            return await graph.ainvoke(input)
    except SagaAborted as aborted:
        if reraise:
            raise
        return aborted.report


__all__ = ["wrap_tool", "saga_run", "build_runner"]
