"""The `@saga` boundary and the `@tool` registration decorator."""

from __future__ import annotations

import contextvars
import functools
import inspect
from typing import Any, Callable, Optional

from .context import RollbackReport, SagaAborted, SagaContext
from .gate import PreFlightGate
from .semantics import ActionSemantics, CompensationFactory
from .wal import AsyncWAL

_current: contextvars.ContextVar[Optional[SagaContext]] = contextvars.ContextVar(
    "agent_saga_current", default=None
)


def current_saga() -> Optional[SagaContext]:
    """The active saga for this task, or None. contextvars rather than a global
    so concurrent agents in one process do not share a compensation stack."""
    return _current.get()


def saga(
    _fn: Optional[Callable] = None,
    *,
    gate: Optional[PreFlightGate] = None,
    wal: Optional[AsyncWAL] = None,
    halt_on_compensation_failure: bool = True,
    reraise: bool = True,
):
    """Marks a transactional boundary.

    On any exception inside the boundary, compensations run LIFO and the
    original exception is re-raised wrapped in `SagaAborted`, which carries the
    `RollbackReport`. Callers must be able to tell a clean rollback from a
    partial one -- swallowing that distinction is the failure mode this library
    exists to prevent.
    """

    def decorate(fn: Callable) -> Callable:
        if not inspect.iscoroutinefunction(fn):
            raise TypeError(f"@saga requires an async function; {fn.__qualname__} is sync")

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            own_wal = wal is None
            _wal = wal or AsyncWAL()
            if own_wal:
                await _wal.start()
            ctx = SagaContext(
                gate=gate,
                wal=_wal,
                halt_on_compensation_failure=halt_on_compensation_failure,
            )
            token = _current.set(ctx)
            await ctx.begin()
            try:
                result = await fn(*args, **kwargs)
            except BaseException as exc:
                report = await ctx.rollback()
                # SAGA_ABORTED is what tells the recovery daemon this saga was
                # resolved in-process and must not be touched again.
                await ctx.finish(aborted=True, clean=report.clean)
                if reraise:
                    raise SagaAborted(exc, report) from exc
                return report
            else:
                await ctx.finish()
                return result
            finally:
                _current.reset(token)
                if own_wal:
                    await _wal.close()

        wrapper.__wrapped_saga__ = True  # type: ignore[attr-defined]
        return wrapper

    return decorate(_fn) if _fn is not None else decorate


def tool(
    name: Optional[str] = None,
    *,
    semantics: ActionSemantics,
    compensate: Optional[CompensationFactory] = None,
    timeout: Optional[float] = None,
):
    """Registers a function as a saga-aware tool.

    Outside a saga boundary the call passes through untouched, so the same tool
    is usable in tests and one-off scripts without ceremony.
    """

    def decorate(fn: Callable) -> Callable:
        tool_name = name or fn.__name__

        @functools.wraps(fn)
        async def wrapper(**kwargs) -> Any:
            ctx = current_saga()
            if ctx is None:
                from .context import _invoke

                return await _invoke(fn, kwargs, timeout)
            return await ctx.execute(
                tool=tool_name,
                semantics=semantics,
                forward=fn,
                forward_kwargs=kwargs,
                compensate=compensate,
                timeout=timeout,
            )

        wrapper.__saga_tool__ = {"name": tool_name, "semantics": semantics}  # type: ignore[attr-defined]
        return wrapper

    return decorate


__all__ = ["saga", "tool", "current_saga", "SagaAborted", "RollbackReport"]
