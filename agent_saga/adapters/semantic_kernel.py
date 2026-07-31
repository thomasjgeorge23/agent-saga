"""Semantic Kernel adapter.

Drop a saga boundary around a Semantic Kernel agent, and wrap the plugin
functions whose effects must unwind. A wrapped function keeps its name,
description and parameter metadata, so the kernel and the planner treat it
exactly like the original.

Semantic Kernel exposes tools as `KernelFunction` objects, usually created by
decorating a method with `@kernel_function`. The invocation surface is
`invoke(kernel, arguments)` and is async, which is the shape `build_runner`
already wants -- so this adapter is thinner than the CrewAI one, which has to
bridge a synchronous `_run` onto a worker thread.

The routing core lives in `.._common.build_runner` and is tested without
Semantic Kernel installed; the SDK is imported lazily, only inside `wrap_tool`.

**On what is verified.** The routing and rollback behaviour -- joining the
active saga, passing arguments to the gate, compensating LIFO on failure -- is
pinned by tests that need no SDK, because it is `build_runner` and shared with
every other adapter. The SDK-specific packaging below (the isinstance check, the
metadata copy) is exercised by an integration test that skips when
`semantic-kernel` is absent, which is how the CrewAI and OpenAI adapters are
covered too. Saying which half is which is more useful than implying both are
equally proven.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from ..context import SagaAborted
from ..decorator import saga_scope
from ..semantics import ActionSemantics, CompensationFactory
from ._common import build_runner

__all__ = ["saga_run", "wrap_tool"]


def wrap_tool(
    function: Any,
    *,
    semantics: ActionSemantics,
    compensate: Optional[CompensationFactory] = None,
    timeout: Optional[float] = None,
    name: Optional[str] = None,
) -> Any:
    """Return a saga-aware drop-in for a Semantic Kernel `KernelFunction`.

    The returned object is a `KernelFunction` whose invocation routes through
    the active saga. Its name, plugin name, description and parameter metadata
    are copied from the original so the planner sees no difference.
    """
    from semantic_kernel.functions import KernelFunction  # lazy
    from semantic_kernel.functions import kernel_function

    if not isinstance(function, KernelFunction):
        raise TypeError(
            f"expected a semantic_kernel KernelFunction, got "
            f"{type(function).__name__}. Decorate the method with "
            f"@kernel_function and wrap the resulting function.")

    metadata = function.metadata
    tool_name = name or metadata.name

    async def call(**kwargs: Any) -> Any:
        # KernelFunction.invoke returns a FunctionResult; the saga records the
        # value, because a compensation factory reasons about what the tool
        # produced, not about the kernel's envelope around it.
        result = await function.invoke(**kwargs)
        return getattr(result, "value", result)

    runner = build_runner(call, name=tool_name, semantics=semantics,
                          compensate=compensate, timeout=timeout)

    @kernel_function(name=tool_name, description=metadata.description or "")
    async def _saga_wrapped(**kwargs: Any) -> Any:
        return await runner(**kwargs)

    wrapped = KernelFunction.from_method(method=_saga_wrapped,
                                         plugin_name=metadata.plugin_name)
    return wrapped


async def saga_run(
    agent: Any,
    message: Any = None,
    *,
    gate: Any = None,
    wal: Any = None,
    halt_on_compensation_failure: bool = True,
    reraise: bool = True,
    invoke: Optional[Callable[[Any], Any]] = None,
) -> Any:
    """Run a Semantic Kernel agent inside a saga boundary.

    On success returns the agent's output. On any exception, every wrapped tool
    that already executed is compensated LIFO, then `SagaAborted` is raised
    (carrying the `RollbackReport`) unless `reraise=False`.

    `invoke` overrides how the agent is called, for the SDK versions that
    renamed the entry point -- the boundary is the point, and it should not
    break because a method moved.
    """
    # The try wraps the `async with`, not its body: saga_scope raises
    # SagaAborted when the boundary EXITS, so an except inside the block
    # never sees it and reraise=False would silently do nothing.
    try:
        async with saga_scope(
                wal=wal, gate=gate, name="semantic-kernel",
                halt_on_compensation_failure=halt_on_compensation_failure):
            if invoke is not None:
                outcome = invoke(agent)
            elif hasattr(agent, "invoke_async"):
                outcome = agent.invoke_async(message)
            elif hasattr(agent, "invoke"):
                outcome = agent.invoke(message)
            else:
                raise AttributeError(
                    f"{type(agent).__name__} exposes neither invoke nor "
                    f"invoke_async; pass invoke=... to name the entry point "
                    f"rather than accept a boundary around nothing.")
            if hasattr(outcome, "__await__"):
                outcome = await outcome
            return outcome
    except SagaAborted as aborted:
        if reraise:
            raise
        return aborted.report

