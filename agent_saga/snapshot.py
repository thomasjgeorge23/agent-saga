"""Snapshot-based REVERSIBLE steps.

The connectors (Stripe, Postgres, Salesforce) are COMPENSABLE: they touch
shared, durable state that other observers can see, so their inverse is a new,
visible action. This module covers the other end of the spectrum -- state that
is *private to the saga*: an in-process object, a dict the agent is assembling,
a scratch structure no other reader can observe.

For that state, "undo" is exact and needs no hand-written inverse. We capture a
deep copy before the mutation and restore it on rollback. The developer writes
the forward mutation and nothing else; the compensation is derived from the
snapshot.

Two properties fall out of capturing *before* the forward call, and both matter:

  1. Restore is valid even on an UNKNOWN outcome. A Stripe charge that times out
     leaves no id, so nothing can be refunded by id. A snapshot captured before
     the mutation restores correctly whether the mutation half-applied, fully
     applied, or raised. The inverse does not depend on the forward result.

  2. It is legitimately REVERSIBLE, so it rides the WAL fast path (no fsync). The
     justification is not performance -- it is correctness: in-process state does
     not survive a crash, so there is no orphan for saga-recoveryd to resolve. A
     process that dies takes both the effect and the need to undo it with it.

If your "private" state is actually durable (a scratch table, a file the saga
owns), it is NOT this case: a crash leaves it behind, so it needs a
registry-backed COMPENSABLE handler, not an in-process closure. Reach for the
connector pattern there.
"""

from __future__ import annotations

import copy
from typing import Any, Callable, Optional, Protocol, Sequence, runtime_checkable

from .semantics import ActionSemantics, Compensation


@runtime_checkable
class SnapshotStrategy(Protocol):
    """How to read a target's prior state and put it back exactly."""

    def capture(self, target: Any) -> Any: ...
    def restore(self, target: Any, snapshot: Any) -> None: ...


class MappingSnapshot:
    """dict-like targets, restored by identity of contents.

    Restore is clear-and-repopulate, not a shallow overwrite. This is the whole
    correctness argument: a mutation that ADDED a key must have that key removed
    on restore, and a mutation that DELETED a key must have it put back. Merging
    the snapshot over the current state would leave the added key in place -- a
    silent, partial "undo" that is worse than none.
    """

    def capture(self, target: Any) -> dict:
        return copy.deepcopy(dict(target))

    def restore(self, target: Any, snapshot: dict) -> None:
        target.clear()
        target.update(copy.deepcopy(snapshot))


class SequenceSnapshot:
    """list-like targets restored in place, so aliases to the same list see the
    restored contents."""

    def capture(self, target: Any) -> list:
        return copy.deepcopy(list(target))

    def restore(self, target: Any, snapshot: list) -> None:
        target[:] = copy.deepcopy(snapshot)


class SetSnapshot:
    def capture(self, target: Any) -> set:
        return copy.deepcopy(set(target))

    def restore(self, target: Any, snapshot: set) -> None:
        target.clear()
        target.update(copy.deepcopy(snapshot))


class AttributeSnapshot:
    """Named attributes of an object. Only the listed attributes are captured
    and restored -- everything else on the object is left untouched, which is
    what you want when the agent mutates two fields of a larger model."""

    def __init__(self, attributes: Sequence[str]):
        if not attributes:
            raise ValueError("AttributeSnapshot requires at least one attribute name")
        self.attributes = tuple(attributes)

    def capture(self, target: Any) -> dict:
        missing = [a for a in self.attributes if not hasattr(target, a)]
        if missing:
            raise AttributeError(f"target is missing attribute(s): {missing}")
        return {a: copy.deepcopy(getattr(target, a)) for a in self.attributes}

    def restore(self, target: Any, snapshot: dict) -> None:
        for attr, value in snapshot.items():
            setattr(target, attr, copy.deepcopy(value))


def auto_strategy(target: Any) -> SnapshotStrategy:
    """Pick a strategy from the target's shape. Ordering matters: dict before
    the generic Mapping check, and str/bytes are deliberately excluded from the
    sequence branch."""
    import collections.abc as abc

    if isinstance(target, abc.MutableMapping):
        return MappingSnapshot()
    if isinstance(target, abc.MutableSet):
        return SetSnapshot()
    if isinstance(target, (str, bytes)):
        raise TypeError(
            "immutable target has no in-place mutation to reverse; snapshot the "
            "object that holds it instead (e.g. AttributeSnapshot)"
        )
    if isinstance(target, abc.MutableSequence):
        return SequenceSnapshot()
    raise TypeError(
        f"no automatic snapshot strategy for {type(target).__name__}; pass "
        f"strategy=AttributeSnapshot([...]) or a custom SnapshotStrategy"
    )


async def reversible(
    ctx,
    *,
    target: Any,
    mutate: Callable[[Any], Any],
    strategy: Optional[SnapshotStrategy] = None,
    tool: str = "memory.mutate",
) -> Any:
    """Run a mutation of private in-process state, capturing its prior form as
    the exact inverse.

        cart = {"items": [], "total": 0}
        await reversible(ctx, target=cart,
                         mutate=lambda c: c.update(items=["sku_1"], total=42))
        # on rollback, cart is exactly {"items": [], "total": 0} again

    `mutate` receives the target and may return anything; the return value is
    passed back to the caller. The snapshot is taken before `mutate` runs, so
    the restore is correct even if `mutate` raises partway through.
    """
    strat = strategy or auto_strategy(target)

    # Captured BEFORE the forward call -- this is the ordering the whole module
    # depends on. Deep-copied so a later in-place mutation of `target` cannot
    # reach back and corrupt the snapshot.
    snapshot = strat.capture(target)

    def _forward() -> Any:
        return mutate(target)

    def _compensate(_result: Any) -> Compensation:
        # Note: we ignore `_result` and the UNKNOWN/None case entirely. The
        # inverse was fully determined before the forward call ran, so it is
        # valid regardless of how the forward call ended.
        return Compensation(
            fn=lambda: strat.restore(target, snapshot),
            description=f"restore {tool} snapshot ({type(target).__name__})",
        )

    return await ctx.execute(
        tool=tool,
        semantics=ActionSemantics.REVERSIBLE,
        forward=_forward,
        compensate=_compensate,
    )


__all__ = [
    "SnapshotStrategy",
    "MappingSnapshot",
    "SequenceSnapshot",
    "SetSnapshot",
    "AttributeSnapshot",
    "auto_strategy",
    "reversible",
]
