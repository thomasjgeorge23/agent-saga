"""Named compensation handlers.

A closure cannot survive a SIGKILL. `lambda: refund("ch_1")` is perfectly good
for in-process rollback and completely useless to a recovery daemon in another
process, which has only the WAL to work from.

So a compensation is recoverable if and only if:
  1. it names a handler registered in this table, and
  2. its kwargs survive a JSON round trip.

Both are checked when the compensation is created, not when it is needed. You
learn that a step is unrecoverable while everything is still fine -- never at
3am from a daemon that cannot fix it.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

_HANDLERS: dict[str, Callable[..., Any]] = {}


def compensator(name: str) -> Callable[[Callable], Callable]:
    """Register a named, cross-process-resolvable compensation handler.

        @compensator("stripe.refund")
        def refund(charge_id: str, idempotency_key: str): ...
    """

    def decorate(fn: Callable) -> Callable:
        if name in _HANDLERS and _HANDLERS[name] is not fn:
            raise ValueError(
                f"compensation handler {name!r} is already registered to "
                f"{_HANDLERS[name]!r}; names must be stable across deploys "
                f"or in-flight sagas become unrecoverable"
            )
        _HANDLERS[name] = fn
        fn.__compensator_name__ = name  # type: ignore[attr-defined]
        return fn

    return decorate


def resolve(name: str) -> Optional[Callable[..., Any]]:
    return _HANDLERS.get(name)


def registered() -> tuple[str, ...]:
    return tuple(sorted(_HANDLERS))


def json_roundtrips(value: Any) -> bool:
    """Kwargs that only *repr* cleanly are a trap: they serialize without error
    and deserialize into a string that the handler cannot use."""
    try:
        return json.loads(json.dumps(value)) == value
    except (TypeError, ValueError):
        return False


__all__ = ["compensator", "resolve", "registered", "json_roundtrips"]
