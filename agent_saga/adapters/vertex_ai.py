"""Vertex AI adapter (Gemini function calling / Vertex AI Agent Engine).

Vertex AI does not execute tools for you. The model returns a function call, and
your code decides whether to run it -- which is exactly the moment a saga
boundary belongs, and exactly the moment most integrations skip straight past.

Two entry points, for the two shapes Vertex work takes:

  * `wrap_tool(fn, ...)` -- takes the plain Python callable you would have
    handed to `FunctionDeclaration`/`Tool`, and returns a drop-in whose
    execution is recorded on the active saga. The signature, name and docstring
    are preserved, so the schema Vertex generates from it is unchanged.
  * `dispatch_function_call(call, registry, ...)` -- takes the
    `FunctionCall` a Gemini response carries and runs the matching wrapped
    tool inside the saga. This is the piece that is genuinely easy to get wrong:
    a model can name a function that does not exist, and the natural handling
    (`registry[call.name]`) raises `KeyError` deep in a response loop. Here an
    unknown name is refused by name, before anything runs.

The routing core is `.._common.build_runner`, shared with every other adapter
and tested without any Google SDK installed. `google-cloud-aiplatform` is
imported lazily and only where a real Vertex type is genuinely needed --
`wrap_tool` needs none of it, so the common case works with the SDK absent.

**On what is verified.** Routing, gating and LIFO rollback are pinned by tests
that need no SDK. The `FunctionCall` unpacking is exercised against both a real
proto (skipped when the SDK is missing) and a duck-typed stand-in, because the
attribute shape has been stable across versions while the import path has not.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Mapping, Optional

from ..context import SagaAborted
from ..decorator import saga_scope
from ..semantics import ActionSemantics, CompensationFactory
from ._common import build_runner

__all__ = ["UnknownFunctionCall", "dispatch_function_call", "saga_run", "wrap_tool"]


class UnknownFunctionCall(LookupError):
    """The model asked for a function that is not registered.

    Its own exception type because it is not a bug in your code -- it is the
    model hallucinating a tool, which is routine, and a response loop should be
    able to catch precisely that and reply to the model rather than crash.
    """


def wrap_tool(
    function: Callable[..., Any],
    *,
    semantics: ActionSemantics,
    compensate: Optional[CompensationFactory] = None,
    timeout: Optional[float] = None,
    name: Optional[str] = None,
) -> Callable[..., Any]:
    """Return a saga-aware drop-in for a Vertex AI tool function.

    Vertex builds its schema by introspecting the callable, so the returned
    coroutine keeps the original's `__name__`, `__doc__`, signature and
    annotations. The declaration you generate from it is identical.
    """
    import functools
    import inspect

    tool_name = name or getattr(function, "__name__", "tool")

    async def call(**kwargs: Any) -> Any:
        result = function(**kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    runner = build_runner(call, name=tool_name, semantics=semantics,
                          compensate=compensate, timeout=timeout)

    @functools.wraps(function)
    async def wrapped(**kwargs: Any) -> Any:
        return await runner(**kwargs)

    # functools.wraps copies __name__/__doc__/__wrapped__; the signature follows
    # via __wrapped__, which is what inspect.signature and therefore Vertex's
    # schema generation read.
    setattr(wrapped, "__agent_saga_tool__", tool_name)
    return wrapped


async def dispatch_function_call(
    call: Any,
    registry: Mapping[str, Callable[..., Any]],
    *,
    strict: bool = True,
) -> Any:
    """Execute the tool a Gemini `FunctionCall` names, inside the active saga.

    `registry` maps function name to a callable already wrapped by `wrap_tool`.
    An unknown name raises `UnknownFunctionCall` naming what was asked for and
    what exists, rather than a bare `KeyError` from inside a response loop.

    With `strict=False` an unknown name returns a structured error dict instead
    of raising, which is the shape you want when feeding the model back its own
    mistake so it can correct itself.
    """
    name = getattr(call, "name", None)
    if not name:
        raise UnknownFunctionCall(
            f"{type(call).__name__} carries no function name; expected a Vertex "
            f"FunctionCall with a .name")

    arguments = _arguments(call)

    if name not in registry:
        message = (f"the model called {name!r}, which is not registered. "
                   f"Available: {sorted(registry)}")
        if strict:
            raise UnknownFunctionCall(message)
        return {"error": "unknown_function", "detail": message}

    tool = registry[name]
    result = tool(**arguments)
    if hasattr(result, "__await__"):
        return await result
    return result


async def saga_run(
    runner: Callable[[], Any],
    *,
    gate: Any = None,
    wal: Any = None,
    halt_on_compensation_failure: bool = True,
    reraise: bool = True,
) -> Any:
    """Run a Vertex AI turn (or a whole multi-turn loop) inside a saga boundary.

    `runner` is a zero-argument callable performing the work -- typically the
    generate/dispatch loop. Every wrapped tool it executes joins this saga, and
    any exception unwinds them LIFO before `SagaAborted` propagates.
    """
    # The try wraps the `async with`, not its body: saga_scope raises
    # SagaAborted when the boundary EXITS, so an except inside the block
    # never sees it and reraise=False would silently do nothing.
    try:
        async with saga_scope(
                wal=wal, gate=gate, name="vertex-ai",
                halt_on_compensation_failure=halt_on_compensation_failure):
            outcome = runner()
            if hasattr(outcome, "__await__"):
                outcome = await outcome
            return outcome
    except SagaAborted as aborted:
        if reraise:
            raise
        return aborted.report


def _arguments(call: Any) -> Dict[str, Any]:
    """Unpack a FunctionCall's args.

    Vertex returns a protobuf `Struct`-backed mapping. It behaves like a dict,
    but `dict(...)` on the proto wrapper is the conversion that actually works
    across SDK versions -- hence the explicit coercion rather than assuming a
    plain dict arrived.
    """
    args = getattr(call, "args", None)
    if args is None:
        return {}
    try:
        return {str(key): args[key] for key in args}
    except TypeError:
        try:
            return dict(args)
        except Exception:
            return {}
