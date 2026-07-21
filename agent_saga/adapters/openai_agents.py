"""OpenAI Agents SDK adapter (the `openai-agents` package).

Wrap a `FunctionTool` so its execution is recorded on the active saga, and run
an agent inside a saga boundary.

The wrinkle unique to this SDK: a tool is invoked as
`on_invoke_tool(run_context, arguments_json_string)` -- the arguments arrive as
a JSON *string*, not kwargs. The wrapper parses that string so the pre-flight
gate can see the actual argument values (a threshold rule needs the number, not
an opaque blob), then routes through the saga and calls the original tool.

The routing core is `.._common.build_runner`, tested without the SDK installed;
`agents` is imported lazily.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any, Callable, Optional

from ..context import SagaAborted
from ..decorator import saga_scope
from ..semantics import ActionSemantics, CompensationFactory
from ._common import build_runner


def _parse_args(arguments: Any) -> dict:
    """Best-effort: turn the SDK's JSON argument string into kwargs for policy.
    Never raises on the routing path -- a tool with unparseable args still runs,
    it just reaches the gate with no policy_args."""
    if isinstance(arguments, dict):
        return arguments
    if not arguments:
        return {}
    try:
        parsed = json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {"input": parsed}


def wrap_tool(
    tool: Any,
    *,
    semantics: ActionSemantics,
    compensate: Optional[CompensationFactory] = None,
    timeout: Optional[float] = None,
    name: Optional[str] = None,
) -> Any:
    """Return a saga-aware copy of a `FunctionTool`.

    Everything but the invocation hook is copied verbatim (name, description,
    JSON schema, strictness), so the model and the runner see the same tool.
    """
    from agents import FunctionTool  # lazy import, validates the type

    if not isinstance(tool, FunctionTool):
        raise TypeError(
            f"expected an agents.FunctionTool, got {type(tool).__name__}. Build it "
            f"with @function_tool and wrap the result."
        )

    tool_name = name or tool.name
    original_invoke = tool.on_invoke_tool

    async def _on_invoke_tool(run_context: Any, arguments: Any) -> Any:
        # The saga's forward call re-invokes the original with the SDK's own
        # (run_context, arguments); the parsed kwargs are only for the gate.
        async def _call(**_ignored: Any) -> Any:
            return await original_invoke(run_context, arguments)

        runner = build_runner(
            _call, name=tool_name, semantics=semantics,
            compensate=compensate, timeout=timeout,
        )
        return await runner(**_parse_args(arguments))

    # dataclasses.replace keeps params_json_schema, strict_json_schema, etc.
    return dataclasses.replace(tool, name=tool_name, on_invoke_tool=_on_invoke_tool)


async def saga_run(
    agent: Any,
    input: Any = None,
    *,
    gate: Any = None,
    wal: Any = None,
    halt_on_compensation_failure: bool = True,
    reraise: bool = True,
    run: Optional[Callable[[Any], Any]] = None,
    **run_kwargs: Any,
) -> Any:
    """Run an agent (via `Runner.run`) inside a saga boundary.

    On success returns the run result. On any exception -- including a guardrail
    tripwire or a tool raising -- every wrapped tool that executed is compensated
    LIFO, then `SagaAborted` is raised unless `reraise=False`.

    `run` overrides how the agent is driven: pass a coroutine `(saga_context) ->
    result` to use `Runner.run_streamed`, a custom `RunConfig`, or sessions.
    Extra `**run_kwargs` are forwarded to `Runner.run`.
    """
    try:
        async with saga_scope(
            gate=gate, wal=wal,
            halt_on_compensation_failure=halt_on_compensation_failure,
        ) as ctx:
            if run is not None:
                return await run(ctx)
            from agents import Runner  # lazy

            return await Runner.run(agent, input, **run_kwargs)
    except SagaAborted as aborted:
        if reraise:
            raise
        return aborted.report


__all__ = ["wrap_tool", "saga_run"]
