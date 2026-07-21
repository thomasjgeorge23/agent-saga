"""The `@saga` boundary and the `@tool` registration decorator."""

from __future__ import annotations

import contextlib
import contextvars
import functools
import inspect
from typing import Any, AsyncIterator, Callable, Optional

from .context import RollbackReport, SagaAborted, SagaContext
from .gate import PreFlightGate
from .semantics import ActionSemantics, CompensationFactory
from .wal import AsyncWAL

_current: contextvars.ContextVar[Optional[SagaContext]] = contextvars.ContextVar(
    "agent_saga_current", default=None
)

_DEFAULT_WAL: Optional[AsyncWAL] = None
_active_sagas: set[SagaContext] = set()


def get_default_wal() -> Optional[AsyncWAL]:
    """Retrieve the process-wide default write-ahead log."""
    return _DEFAULT_WAL


def set_default_wal(wal: Optional[AsyncWAL]) -> None:
    """Set the process-wide default write-ahead log."""
    global _DEFAULT_WAL
    _DEFAULT_WAL = wal


def current_saga() -> Optional[SagaContext]:
    """The active saga for this task, or None. contextvars rather than a global
    so concurrent agents in one process do not share a compensation stack."""
    return _current.get()


@contextlib.asynccontextmanager
async def saga_scope(
    *,
    gate: Optional[PreFlightGate] = None,
    wal: Optional[AsyncWAL] = None,
    halt_on_compensation_failure: bool = True,
) -> AsyncIterator[SagaContext]:
    """The one transactional boundary. `@saga`, `saga_run`, and any framework
    adapter all go through here, so the begin/rollback/finish/lease lifecycle
    lives in exactly one place.

    On any exception inside the scope, compensations run LIFO and a `SagaAborted`
    is raised carrying the `RollbackReport`. A caller that wants the report
    instead of the exception catches `SagaAborted` and reads `.report`.
    """
    own_wal = wal is None and _DEFAULT_WAL is None
    _wal = wal or _DEFAULT_WAL or AsyncWAL()
    if own_wal:
        await _wal.start()
    ctx = SagaContext(gate=gate, wal=_wal,
                      halt_on_compensation_failure=halt_on_compensation_failure)
    _active_sagas.add(ctx)
    token = _current.set(ctx)
    await ctx.begin()
    try:
        yield ctx
    except BaseException as exc:
        # Capture *why* before unwinding -- this is the only place the trigger
        # is known. Written ahead of ROLLBACK_START so the trace reads causally.
        ctx.record_abort(exc)
        report = await ctx.rollback()
        # SAGA_ABORTED tells saga-recoveryd this saga was resolved in-process
        # and must not be touched again.
        await ctx.finish(aborted=True, clean=report.clean)
        raise SagaAborted(exc, report) from exc
    else:
        await ctx.finish()
    finally:
        _current.reset(token)
        _active_sagas.discard(ctx)
        if own_wal:
            await _wal.close()


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
            try:
                async with saga_scope(
                    gate=gate, wal=wal,
                    halt_on_compensation_failure=halt_on_compensation_failure,
                ):
                    return await fn(*args, **kwargs)
            except SagaAborted as aborted:
                if reraise:
                    raise
                return aborted.report

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


__all__ = ["saga", "saga_scope", "tool", "current_saga", "SagaAborted", "RollbackReport"]
