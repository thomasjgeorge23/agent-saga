"""Typed compensation semantics.

The central claim of this library: "undo" is not one thing. It is three
things with materially different risk profiles, and the difference is what
a risk committee actually buys.
"""

from __future__ import annotations

import enum
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional


class ActionSemantics(enum.Enum):
    """How cleanly the world can be returned to its prior state."""

    REVERSIBLE = "REVERSIBLE"
    """State can be restored exactly. No observer can tell it happened.
    e.g. a row update where we hold the prior snapshot, an in-memory cache write."""

    COMPENSABLE = "COMPENSABLE"
    """The effect can be semantically offset, but leaves a permanent trace.
    e.g. a Stripe refund, closing an issue we opened, deleting a Slack message.
    The ledger now shows two events, not zero."""

    IRREVERSIBLE = "IRREVERSIBLE"
    """No automated action restores or offsets the effect.
    e.g. an outbound email, a wire transfer, DROP TABLE without a snapshot.
    These must be gated *before* execution, never cleaned up after."""


class StepState(enum.Enum):
    """Lifecycle of a single saga step."""

    INTENT_LOGGED = "INTENT_LOGGED"      # durable record written, not yet executed
    COMMITTED = "COMMITTED"              # forward call returned successfully
    UNKNOWN = "UNKNOWN"                  # forward call raised/timed out; effect MAY have landed
    COMPENSATED = "COMPENSATED"          # compensation ran successfully
    COMPENSATION_FAILED = "COMPENSATION_FAILED"
    ORPHANED = "ORPHANED"                # IRREVERSIBLE, executed, cannot be undone
    UNRESOLVED = "UNRESOLVED"            # rollback halted before reaching this step
    COMPLETED_VIA_FALLBACK = "COMPLETED_VIA_FALLBACK" # completed using a fallback action


@dataclass(frozen=True)
class Compensation:
    """A concrete, runtime-derived inverse action.

    This is the Temporal wedge. Temporal makes you declare the compensating
    step at authoring time. Here the *agent* chose the forward action, so the
    inverse is only knowable once the forward call returns and we know what
    it actually touched (the charge id, the row ids, the message ts).
    """

    fn: Callable[..., Any]
    kwargs: dict = field(default_factory=dict)
    description: str = ""
    idempotency_key: str = field(default_factory=lambda: uuid.uuid4().hex)
    """Compensation may be retried, or run after an UNKNOWN forward call.
    Connectors must key on this so a double-refund is impossible."""
    handler: Optional[str] = None
    """Name in the compensation registry. Without it this compensation works
    in-process and is lost forever if the process dies. See registry.py."""

    @property
    def recoverable(self) -> bool:
        """Can a different process, reading only the WAL, run this?

        The JSON round trip is real work on the hot path, and both describe()
        and the unrecoverable-warning ask for it -- so compute it once per
        compensation and cache it on the (frozen) instance.
        """
        cached = getattr(self, "_recoverable_cache", None)
        if cached is None:
            from .registry import json_roundtrips

            cached = bool(self.handler) and json_roundtrips(self.kwargs)
            object.__setattr__(self, "_recoverable_cache", cached)
        return cached

    def describe(self) -> dict:
        """WAL-safe projection. Callables are not serializable; their intent is."""
        recoverable = self.recoverable
        return {
            "fn": getattr(self.fn, "__qualname__", repr(self.fn)),
            "handler": self.handler,
            "recoverable": recoverable,
            "description": self.description,
            "idempotency_key": self.idempotency_key,
            # Only emit kwargs we know survive the round trip. A repr()'d dict
            # that deserializes into a string is worse than an absent one.
            "kwargs": self.kwargs if recoverable else {k: _safe(v) for k, v in self.kwargs.items()},
        }


# A factory, not a callable: it receives the forward result (or None if the
# forward call failed with unknown outcome) and derives the inverse.
CompensationFactory = Callable[[Any], Optional[Compensation]]


@dataclass
class SagaStep:
    tool: str
    semantics: ActionSemantics
    state: StepState = StepState.INTENT_LOGGED
    compensation: Optional[Compensation] = None
    step_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started_at: float = field(default_factory=time.monotonic)
    error: Optional[str] = None

    @property
    def needs_compensation(self) -> bool:
        """UNKNOWN counts. A timed-out charge may well have landed."""
        return self.state in (StepState.COMMITTED, StepState.UNKNOWN)


def _safe(value: Any) -> Any:
    """Best-effort JSON projection. Never raises on the logging path."""
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    return repr(value)[:512]


__all__ = [
    "ActionSemantics",
    "StepState",
    "Compensation",
    "CompensationFactory",
    "SagaStep",
]
