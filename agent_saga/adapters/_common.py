"""Framework-agnostic saga routing shared by the connector adapters.

Every adapter reduces to the same thing: given some way to actually call the
underlying tool, produce a coroutine that -- inside a saga -- routes through
`SagaContext.execute` (gate, WAL, compensation) and -- outside one -- calls the
tool untouched. Only the packaging around this differs per framework, and that
packaging is where the lazy framework import lives.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from ..decorator import current_saga
from ..semantics import ActionSemantics, CompensationFactory


def build_runner(
    call: Callable[..., Awaitable[Any]],
    *,
    name: str,
    semantics: ActionSemantics,
    compensate: Optional[CompensationFactory] = None,
    timeout: Optional[float] = None,
) -> Callable[..., Awaitable[Any]]:
    """Wrap an async `call(**kwargs)` so it records on the active saga.

    Passes the tool arguments as `policy_args` so pre-flight threshold rules can
    see them -- an argument captured only in the forward closure is invisible to
    the gate, the exact bug that once let a connector bypass a limit.
    """

    async def _run(**kwargs: Any) -> Any:
        async def _forward() -> Any:
            return await call(**kwargs)

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

    _run.__name__ = f"saga_{name}".replace(".", "_").replace("-", "_")
    return _run


__all__ = ["build_runner"]
