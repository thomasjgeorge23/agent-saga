"""Tentative state: make "not yet confirmed" a first-class, visible status.

A saga commits each step as it goes, so there is no isolation. The classic
failure is a balance: step 1 debits an account, step 3 fails, and in between a
second reader saw money that was about to come back. You cannot fix that with a
database transaction -- holding one across an LLM's thinking time is exactly
what the saga pattern exists to avoid.

The structural answer is to stop pretending the write is final. A resource
touched mid-saga is marked PENDING; readers that care can see it is in flight
and decide for themselves. When the saga resolves, the resource moves to
COMMITTED or ROLLED_BACK exactly once.

The status transitions are enforced, not advisory: a resource cannot go from
COMMITTED back to PENDING, and cannot be resolved twice. A double resolution
usually means a lifecycle bug, and it would otherwise surface much later as a
mysteriously wrong balance.

Standard library only.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger("agent_saga.patterns.tentative")


class TentativeStatus(enum.Enum):
    PENDING = "PENDING"
    """In flight. The write is visible, but not confirmed -- a reader that cares
    about correctness should treat it as provisional."""

    COMMITTED = "COMMITTED"
    """The saga succeeded. The value is final."""

    ROLLED_BACK = "ROLLED_BACK"
    """The saga failed and this was undone. Kept as a distinct status rather
    than deleted, so an auditor can see it happened."""


class TentativeConflictError(RuntimeError):
    """An illegal status transition -- resolving twice, or reopening a resolved
    resource. Raised loudly because the alternative is silent state corruption."""


@dataclass
class TentativeResource:
    """A business entity held in a provisional state for a saga's duration.

    `on_commit` / `on_rollback` are optional callbacks that apply the real
    effect. They are invoked exactly once, by the saga boundary, and a failure
    in one is reported rather than swallowed -- an unconfirmed balance that
    silently stays PENDING is worse than a loud error.
    """

    resource_id: str
    status: TentativeStatus = TentativeStatus.PENDING
    saga_id: Optional[str] = None
    on_commit: Optional[Callable[[], Any]] = None
    on_rollback: Optional[Callable[[], Any]] = None
    rollback_handler: Optional[str] = None
    """Name in the compensation registry, for crash recovery.

    `on_rollback` is a closure: perfect in-process, and completely invisible to a
    daemon in another process. Without a named handler a SIGKILL strands this
    resource in PENDING forever, which is the failure this whole engine exists
    to prevent -- so the absence is warned about, loudly."""
    rollback_kwargs: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

    @property
    def resolved(self) -> bool:
        return self.status is not TentativeStatus.PENDING

    @property
    def recoverable(self) -> bool:
        """Can a different process, reading only the WAL, roll this back?"""
        from ..registry import json_roundtrips

        return bool(self.rollback_handler) and json_roundtrips(self.rollback_kwargs)

    def describe(self) -> dict:
        """WAL projection. Callables are not serializable; their intent is."""
        return {
            "resource_id": self.resource_id,
            "status": self.status.value,
            "rollback_handler": self.rollback_handler,
            "recoverable": self.recoverable,
            "rollback_kwargs": self.rollback_kwargs if self.recoverable else {},
            "metadata": self.metadata,
        }

    @property
    def is_pending(self) -> bool:
        return self.status is TentativeStatus.PENDING

    def _transition(self, to: TentativeStatus) -> None:
        if self.resolved:
            raise TentativeConflictError(
                f"resource {self.resource_id!r} is already {self.status.value}; "
                f"refusing to move it to {to.value}. Resolving twice means the "
                f"saga lifecycle ran twice."
            )
        self.status = to

    async def commit(self) -> None:
        self._transition(TentativeStatus.COMMITTED)
        if self.on_commit is not None:
            await _invoke(self.on_commit)
        logger.info("tentative %s committed", self.resource_id)

    async def rollback(self) -> None:
        self._transition(TentativeStatus.ROLLED_BACK)
        if self.on_rollback is not None:
            await _invoke(self.on_rollback)
        logger.info("tentative %s rolled back", self.resource_id)


async def _invoke(fn: Callable[[], Any]) -> Any:
    """Callbacks may be sync or async. Sync ones go to the bounded tool pool so
    a blocking call cannot stall the event loop during saga teardown."""
    import inspect

    if inspect.iscoroutinefunction(fn):
        return await fn()
    from ..executors import get_tool_executor

    return await get_tool_executor().run(fn)


def tentative(
    ctx,
    resource_id: str,
    *,
    on_commit: Optional[Callable[[], Any]] = None,
    on_rollback: Optional[Callable[[], Any]] = None,
    rollback_handler: Optional[str] = None,
    rollback_kwargs: Optional[dict] = None,
    lock: bool = False,
    **metadata: Any,
) -> TentativeResource:
    """Register a resource as tentative for the duration of this saga.

    Returns immediately in PENDING. The saga boundary resolves it: COMMITTED on
    success, ROLLED_BACK on failure -- no caller has to remember to do it, which
    is the point, since the failure path is the one people forget.

        balance = tentative(saga, "account:usr_123",
                            on_commit=confirm_debit,
                            on_rollback=restore_balance,
                            lock=True)

    `lock=True` also takes a semantic lock on `resource_id`, so a concurrent
    saga cannot touch the same resource while this one is in flight.
    """
    resource = TentativeResource(
        resource_id=resource_id, saga_id=getattr(ctx, "saga_id", None),
        on_commit=on_commit, on_rollback=on_rollback,
        rollback_handler=rollback_handler,
        rollback_kwargs=dict(rollback_kwargs or {}),
        metadata=dict(metadata),
    )
    ctx.register_tentative(resource, lock=lock)
    return resource


__all__ = ["TentativeStatus", "TentativeResource", "TentativeConflictError",
           "tentative"]
