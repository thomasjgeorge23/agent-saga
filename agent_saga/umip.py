"""UMIP -- Universal Multi-agent Interoperability Protocol.

A saga today usually stops at a framework boundary. A LangChain agent books the
van, a CrewAI agent charges the card, an MCP server files the permit -- and when
the permit is refused, nothing unwinds the first two, because each framework
only knows how to undo its own work.

UMIP is the small contract that removes the boundary. Any callable from any
framework can join a saga by declaring three things:

    name        what it is called, in the log and in a compensation
    semantics   REVERSIBLE / COMPENSABLE / IRREVERSIBLE -- what undo means here
    compensate  how to undo it, derived from the forward call's own result

That is the whole protocol. Everything else -- the gate, the WAL, LIFO rollback,
approvals, limits -- is already framework-agnostic, so a participant that
declares those three things is indistinguishable from a native step.

    reg = UMIPRegistry()
    reg.register(Participant("van.book", "langchain", COMPENSABLE, book, undo_book))
    reg.register(Participant("card.charge", "crewai", COMPENSABLE, charge, refund))

    async with saga_scope(name="job-42"):
        await reg.invoke("van.book", when="tuesday")
        await reg.invoke("card.charge", amount=8000)   # fails -> both unwind

Routing goes through the same `build_runner` the native adapters use, so a UMIP
participant and a hand-wrapped LangChain tool take an identical code path. There
is no second implementation to drift.

Across processes, `EntanglementPropagator` carries the saga identity in HTTP
headers, so the same contract spans services, not just frameworks.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional, Sequence

from .adapters._common import build_runner
from .semantics import ActionSemantics, CompensationFactory

logger = logging.getLogger("agent_saga.umip")

UMIP_VERSION = "1.0"


class UMIPConformanceError(ValueError):
    """A participant does not satisfy the protocol."""


@dataclass
class Participant:
    """One framework's callable, described well enough to join a saga."""

    name: str
    framework: str
    semantics: ActionSemantics
    call: Callable[..., Any]
    compensate: Optional[CompensationFactory] = None
    timeout: Optional[float] = None
    description: str = ""

    def describe(self) -> dict:
        return {
            "name": self.name,
            "framework": self.framework,
            "semantics": self.semantics.name,
            "compensating": self.compensate is not None,
            "description": self.description,
        }


def check_conformance(p: Participant) -> None:
    """Enforce the protocol's one real rule, mirroring the engine's own contract:
    a step that claims to be undoable must say how, and a step that cannot be
    undone must not pretend otherwise."""
    if not p.name or not isinstance(p.name, str):
        raise UMIPConformanceError("participant needs a non-empty name")
    if not callable(p.call):
        raise UMIPConformanceError(f"{p.name}: `call` must be callable")
    if not isinstance(p.semantics, ActionSemantics):
        raise UMIPConformanceError(f"{p.name}: semantics must be an ActionSemantics")
    if p.semantics is ActionSemantics.COMPENSABLE and p.compensate is None:
        raise UMIPConformanceError(
            f"{p.name}: declared COMPENSABLE but provides no `compensate`. A step "
            f"that claims to be undoable and is not is the failure this protocol "
            f"exists to prevent.")
    if p.semantics is ActionSemantics.IRREVERSIBLE and p.compensate is not None:
        raise UMIPConformanceError(
            f"{p.name}: declared IRREVERSIBLE but provides a `compensate`. If it "
            f"can be undone it is COMPENSABLE; if it cannot, the compensation is "
            f"a lie the gate would rely on.")


def _as_async(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Present any callable as async. A synchronous framework tool (CrewAI's
    `_run`, a plain Python function) runs on a worker thread so it never blocks
    the loop the other participants are running on."""
    if inspect.iscoroutinefunction(fn):
        return fn

    async def _call(**kwargs: Any) -> Any:
        result = fn(**kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    async def _threaded(**kwargs: Any) -> Any:
        return await asyncio.to_thread(lambda: fn(**kwargs))

    # A sync function that returns an awaitable is a coroutine factory; only
    # genuinely blocking functions need a thread.
    return _call if getattr(fn, "_umip_inline", False) else _threaded


class UMIPRegistry:
    """Participants from several frameworks, invocable as one saga."""

    def __init__(self) -> None:
        self._participants: dict[str, Participant] = {}
        self._runners: dict[str, Callable[..., Any]] = {}

    # -- registration ------------------------------------------------------

    def register(self, participant: Participant) -> Participant:
        check_conformance(participant)
        if participant.name in self._participants:
            raise UMIPConformanceError(
                f"{participant.name!r} is already registered to framework "
                f"{self._participants[participant.name].framework!r}; names must be "
                f"stable, because a WAL record refers to a step by name.")
        self._participants[participant.name] = participant
        self._runners[participant.name] = build_runner(
            _as_async(participant.call),
            name=participant.name,
            semantics=participant.semantics,
            compensate=participant.compensate,
            timeout=participant.timeout,
        )
        logger.debug("UMIP registered %s (%s, %s)",
                     participant.name, participant.framework, participant.semantics.name)
        return participant

    def participant(self, name: str, framework: str, semantics: ActionSemantics, *,
                    compensate: Optional[CompensationFactory] = None,
                    timeout: Optional[float] = None,
                    description: str = "") -> Callable[[Callable], Callable]:
        """Decorator form: register the decorated callable as a participant."""

        def decorate(fn: Callable) -> Callable:
            self.register(Participant(
                name=name, framework=framework, semantics=semantics, call=fn,
                compensate=compensate, timeout=timeout, description=description))
            return fn

        return decorate

    # -- invocation --------------------------------------------------------

    async def invoke(self, name: str, **kwargs: Any) -> Any:
        """Run a participant. Inside a saga it records, gates and becomes
        compensable; outside one it simply calls through."""
        runner = self._runners.get(name)
        if runner is None:
            raise KeyError(
                f"no UMIP participant named {name!r}; registered: "
                f"{', '.join(sorted(self._participants)) or '(none)'}")
        return await runner(**kwargs)

    # -- introspection -----------------------------------------------------

    def get(self, name: str) -> Optional[Participant]:
        return self._participants.get(name)

    def __contains__(self, name: object) -> bool:
        return name in self._participants

    def __len__(self) -> int:
        return len(self._participants)

    def __iter__(self) -> Iterator[Participant]:
        return iter(self._participants.values())

    def frameworks(self) -> list[str]:
        return sorted({p.framework for p in self._participants.values()})

    def manifest(self) -> dict:
        """A machine-readable description of everything that can join a saga
        here -- what a peer needs to know to interoperate."""
        return {
            "umip_version": UMIP_VERSION,
            "frameworks": self.frameworks(),
            "participants": [p.describe() for p in
                             sorted(self._participants.values(), key=lambda x: x.name)],
        }


_REGISTRY: Optional[UMIPRegistry] = None


def get_registry() -> UMIPRegistry:
    """The process-wide default registry."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = UMIPRegistry()
    return _REGISTRY


def set_registry(registry: Optional[UMIPRegistry]) -> None:
    global _REGISTRY
    _REGISTRY = registry


__all__ = [
    "Participant", "UMIPRegistry", "UMIPConformanceError", "check_conformance",
    "get_registry", "set_registry", "UMIP_VERSION",
]
