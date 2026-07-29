"""Cross-framework orchestration you can verify instead of hope about.

One `saga_scope` already spans CrewAI, LangGraph, and AutoGen tools, because
the boundary is a contextvar and every adapter's runner checks
`current_saga()`. `examples/cross_framework.py` shows that working. This module
is what that mechanism needs before it is safe to depend on in production,
because the contextvar has two failure modes and both are silent.

**1. It does not cross a thread boundary.** Measured, not assumed:

    in the coroutine        : True
    asyncio.to_thread       : True     (copies the context)
    raw threading.Thread    : False
    ThreadPoolExecutor      : False

CrewAI and AutoGen commonly run tools on their own executors. When they do,
`current_saga()` returns `None` on that thread, and `build_runner`'s
outside-a-saga path calls the tool **untouched** -- no gate, no WAL record, no
compensation -- and says nothing at all. A developer watching their agent work
would have no way to know the boundary had quietly stopped applying. That is
the `patch_all()` defect again: protection reported but not delivered.

**2. An unwrapped tool looks identical to a wrapped one.** Register twelve
tools across three frameworks, miss one, and nothing anywhere tells you. The
gap surfaces the first time that tool's effect needs undoing.

`SagaFleet` closes both:

  * it **re-attaches** the active saga inside whatever thread a framework
    hands the tool to, so the boundary survives the executor;
  * it **refuses by default** to run a registered tool outside a boundary
    (`require_boundary=True`), because silently unprotected is the one
    outcome worth failing over;
  * it publishes `coverage()` -- a machine-readable manifest of exactly which
    tools are protected, which frameworks they came from, and which have a
    recoverable (registry-backed) inverse versus an in-process closure that
    dies with the process.

`coverage()` is the point. A safety layer nobody can audit is a safety layer
nobody should trust, so the fleet answers "what is actually covered?" with
data rather than a log line.

    fleet = SagaFleet("content-pipeline")
    reserve = fleet.register(crew_run,  framework="crewai",    name="research.reserve",
                             semantics=COMPENSABLE, compensate=release)
    publish = fleet.register(lc_invoke, framework="langgraph", name="writer.publish",
                             semantics=COMPENSABLE, compensate=unpublish)

    print(fleet.coverage().format_text())      # audit before you ship

    async with fleet.transaction(wal=wal):
        await reserve(amount=5000)
        await publish(title="Q3")              # same saga, different framework
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from dataclasses import dataclass, field
from typing import (Any, Awaitable, Callable, Dict, List, Optional, Sequence,
                    Tuple)

from .semantics import ActionSemantics, CompensationFactory

logger = logging.getLogger("agent_saga.fleet")

__all__ = [
    "BoundaryRequired",
    "CoverageReport",
    "SagaFleet",
    "ToolCoverage",
    "bind_saga",
]


class BoundaryRequired(RuntimeError):
    """A tool registered as boundary-required was called with no active saga.

    Raised instead of running the tool. The alternative -- calling it anyway --
    performs a real side effect with no WAL record and no compensation, while
    every log line still says the tool is 'saga-aware'.
    """


def bind_saga(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Carry the active saga into whatever thread `fn` ends up running on.

    `contextvars` propagate into `asyncio.to_thread` but not into a raw
    `threading.Thread` or a `ThreadPoolExecutor.submit`. This captures the
    saga at call time -- on the thread that still has it -- and re-attaches it
    inside the callee, so a framework that dispatches through its own executor
    does not silently drop the boundary.

        pool.submit(bind_saga(my_tool), **kwargs)

    **The saga still belongs to one event loop.** Its WAL runs a flush task
    there, so a coroutine that touches the saga must be driven by that loop:
    from a worker thread, schedule it back with
    `asyncio.run_coroutine_threadsafe(coro, loop)`. Starting a second loop with
    `asyncio.run()` on the worker instead does not raise -- it waits on a future
    belonging to a loop nobody is running, and stalls until the WAL's barrier
    timeout. This function moves the *identity* of the saga across threads, not
    the loop that owns it.
    """
    from .decorator import _current, current_saga

    captured = current_saga()

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if captured is None or current_saga() is not None:
            return fn(*args, **kwargs)
        token = _current.set(captured)
        try:
            return fn(*args, **kwargs)
        finally:
            _current.reset(token)

    wrapper.__name__ = getattr(fn, "__name__", "bound")
    wrapper.__doc__ = getattr(fn, "__doc__", None)
    return wrapper


@dataclass(frozen=True)
class ToolCoverage:
    name: str
    framework: str
    semantics: str
    has_compensation: bool
    boundary_required: bool

    @property
    def concern(self) -> Optional[str]:
        """The one thing wrong with this tool's registration, if any."""
        if self.semantics == "IRREVERSIBLE":
            return None                    # gated before execution; no inverse owed
        if not self.has_compensation:
            return ("no compensation: this effect will be reported ORPHANED on "
                    "rollback")
        if not self.boundary_required:
            return ("boundary not required: called outside a saga it runs "
                    "unprotected and silently")
        return None

    def describe(self) -> dict:
        return {"name": self.name, "framework": self.framework,
                "semantics": self.semantics,
                "has_compensation": self.has_compensation,
                "boundary_required": self.boundary_required,
                "concern": self.concern}


@dataclass(frozen=True)
class CoverageReport:
    fleet: str
    tools: Tuple[ToolCoverage, ...]

    @property
    def frameworks(self) -> Tuple[str, ...]:
        return tuple(sorted({t.framework for t in self.tools}))

    @property
    def concerns(self) -> Tuple[ToolCoverage, ...]:
        return tuple(t for t in self.tools if t.concern)

    @property
    def fully_covered(self) -> bool:
        """Every registered tool can be unwound and cannot run unprotected.
        Strict on purpose: in production you do not get to choose which tool
        is the one that fails."""
        return bool(self.tools) and not self.concerns

    def describe(self) -> dict:
        return {"fleet": self.fleet, "frameworks": list(self.frameworks),
                "tools": len(self.tools), "concerns": len(self.concerns),
                "fully_covered": self.fully_covered,
                "detail": [t.describe() for t in self.tools]}

    def format_text(self) -> str:
        lines = [f"fleet '{self.fleet}': {len(self.tools)} tool(s) across "
                 f"{len(self.frameworks)} framework(s) "
                 f"{list(self.frameworks)}"]
        if not self.tools:
            lines.append("  nothing registered -- this fleet protects nothing")
            return "\n".join(lines)
        for tool in self.tools:
            mark = "  ok  " if not tool.concern else "  !!  "
            lines.append(f"{mark}{tool.framework:<12} {tool.name:<28} "
                         f"{tool.semantics}")
            if tool.concern:
                lines.append(f"        {tool.concern}")
        lines.append("")
        lines.append(f"  fully covered: {self.fully_covered}")
        return "\n".join(lines)


class SagaFleet:
    """One transactional boundary over tools from any number of frameworks."""

    def __init__(self, name: str = "fleet"):
        self.name = name
        self._tools: Dict[str, ToolCoverage] = {}
        self._active: Any = None
        self._lock = threading.Lock()

    # -- registration --------------------------------------------------------------

    def register(
        self,
        call: Callable[..., Awaitable[Any]],
        *,
        name: str,
        framework: str,
        semantics: ActionSemantics,
        compensate: Optional[CompensationFactory] = None,
        timeout: Optional[float] = None,
        require_boundary: bool = True,
    ) -> Callable[..., Awaitable[Any]]:
        """Register an async `call(**kwargs)` and return a saga-aware wrapper.

        `call` is whatever actually invokes the framework's tool -- the same
        thing the per-framework adapters build. `framework` is a free-form
        label used only by `coverage()`, so a mixed fleet can be audited by
        origin.

        `require_boundary=True` (the default) makes the wrapper raise when no
        saga is active. Set it False only for a tool whose effects genuinely do
        not need undoing; the coverage report will flag it either way, because
        the quiet version of this decision is the dangerous one.
        """
        if name in self._tools:
            raise ValueError(
                f"tool {name!r} is already registered in fleet {self.name!r}; "
                f"two tools under one name make the coverage report ambiguous")
        if semantics is not ActionSemantics.IRREVERSIBLE and compensate is None:
            logger.warning(
                "fleet %r: tool %r (%s) has no compensation and will be "
                "reported ORPHANED on rollback", self.name, name, semantics.value)

        self._tools[name] = ToolCoverage(
            name=name, framework=framework, semantics=semantics.value,
            has_compensation=compensate is not None,
            boundary_required=require_boundary)

        async def runner(**kwargs: Any) -> Any:
            from .decorator import _current, current_saga

            ctx = current_saga()
            if ctx is None:
                # The boundary may simply not have crossed a thread. Re-attach
                # the fleet's active saga rather than run unprotected.
                ctx = self._active
                if ctx is not None:
                    token = _current.set(ctx)
                    try:
                        return await self._execute(ctx, name, semantics, call,
                                                   kwargs, compensate, timeout)
                    finally:
                        _current.reset(token)

            if ctx is None:
                if require_boundary:
                    raise BoundaryRequired(
                        f"{name!r} was called with no active saga. It would run "
                        f"with no WAL record and no compensation, so it is "
                        f"refused instead. Open one with "
                        f"`async with fleet.transaction(...)`, or register it "
                        f"with require_boundary=False if this effect genuinely "
                        f"never needs undoing.")
                return await call(**kwargs)

            return await self._execute(ctx, name, semantics, call, kwargs,
                                       compensate, timeout)

        runner.__name__ = f"saga_{name}".replace(".", "_").replace("-", "_")
        runner.__agent_saga_fleet__ = self.name          # type: ignore[attr-defined]
        return runner

    @staticmethod
    async def _execute(ctx, name, semantics, call, kwargs, compensate, timeout):
        async def forward() -> Any:
            return await call(**kwargs)

        return await ctx.execute(
            tool=name, semantics=semantics, forward=forward,
            compensate=compensate,
            # The gate cannot see an argument hidden in a closure, so the tool's
            # arguments are declared explicitly -- the same reason the
            # per-framework adapters pass policy_args.
            policy_args=dict(kwargs),
            timeout=timeout)

    # -- the boundary ------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def transaction(self, *, wal: Any = None, name: Optional[str] = None,
                          gate: Any = None):
        """Open one saga for the whole fleet.

        While it is open, the active saga is also held on the fleet, so a tool
        dispatched onto a framework's own thread can re-attach to it instead of
        finding `current_saga()` empty and running unprotected.

        Not reentrant: nesting would leave `_active` pointing at whichever saga
        exited last, and a tool on a worker thread would then join the wrong
        transaction. That is refused rather than silently resolved.
        """
        from .decorator import saga_scope

        with self._lock:
            if self._active is not None:
                raise RuntimeError(
                    f"fleet {self.name!r} already has an open transaction. "
                    f"Nesting them would let a tool on a worker thread join the "
                    f"wrong saga; use separate fleets for concurrent work.")

        async with saga_scope(wal=wal, gate=gate, name=name or self.name) as saga:
            with self._lock:
                self._active = saga
            try:
                yield saga
            finally:
                with self._lock:
                    self._active = None

    # -- audit -----------------------------------------------------------------------------

    def coverage(self) -> CoverageReport:
        """What this fleet actually protects. Cheap enough for a health check
        and precise enough for a review."""
        return CoverageReport(
            fleet=self.name,
            tools=tuple(sorted(self._tools.values(), key=lambda t: (t.framework, t.name))))

    def assert_fully_covered(self) -> None:
        """Raise unless every registered tool is unwindable and boundary-bound.
        Intended for a startup check or a CI gate."""
        report = self.coverage()
        if not report.fully_covered:
            raise AssertionError("fleet is not fully covered:\n"
                                 + report.format_text())
