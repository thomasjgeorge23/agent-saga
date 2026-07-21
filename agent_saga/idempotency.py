"""Deterministic idempotency guardrails for compensating actions.

A compensation can run more than once. The recovery daemon retries after a
network blip; two daemons race; a process dies between doing the work and
recording that it did. Each of those must end with the effect applied exactly
once, and the mechanism is a key that is *identical* every time.

WHY `attempt_count` IS NOT IN THE KEY
    It is tempting to derive the key from (saga_id, step_id, attempt). It is
    also exactly backwards. An idempotency key works because the *downstream*
    system -- Stripe, your API -- recognises a retry as a duplicate of the
    original request and drops it. If the key changes per attempt, attempt 1
    sends key A and attempt 2 sends key B, so the remote sees two unrelated
    refunds and issues both. Varying the key per attempt guarantees the double
    refund it was meant to prevent.

    So the key is stable across attempts, and the attempt counter lives in the
    ledger as telemetry -- where "we retried this four times" is genuinely worth
    knowing, and where it cannot corrupt the guarantee.

Two layers, because either alone is insufficient:

  1. The key, handed to the remote system, which de-duplicates on its side.
  2. The local execution ledger, so we do not even issue a call we already know
     succeeded -- covering remotes that have no idempotency support at all, and
     saving the round trip for those that do.

Standard library only: hashlib, inspect, json.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger("agent_saga.idempotency")

COMPENSATE_SCOPE = "compensate"

# Journal / WAL events that prove a compensation already completed.
_SUCCESS_EVENTS = frozenset({
    "RECOVERY_SUCCESS",       # written by the recovery daemon
    "COMPENSATION_SUCCESS",   # accepted alias
    "COMPENSATED",            # written by an in-process rollback
})


class IdempotencyManager:
    """Deterministic keys plus the ledger that makes a retry a no-op."""

    # -- key derivation ----------------------------------------------------

    @staticmethod
    def key(saga_id: str, step_id: str, *, scope: str = COMPENSATE_SCOPE) -> str:
        """A stable SHA-256 key for one compensating step.

        Depends only on identifiers that are themselves stable, so any process,
        on any host, at any time, derives the same value -- which is what lets a
        second daemon recognise the first daemon's work.

        `scope` separates distinct operations on the same step (a compensation
        vs. some future re-drive) so they cannot collide.
        """
        material = f"{saga_id}:{step_id}:{scope}".encode("utf-8")
        return hashlib.sha256(material).hexdigest()[:32]

    @classmethod
    def token(cls, saga_id: str, step_id: str) -> str:
        """Alias for the compensation-scoped key, matching the daemon's
        historical `recovery_token` naming."""
        return cls.key(saga_id, step_id, scope=COMPENSATE_SCOPE)

    # -- injection ---------------------------------------------------------

    @staticmethod
    def accepts_key(fn: Callable[..., Any]) -> bool:
        """Whether `fn` can receive an `idempotency_key` kwarg.

        True when it declares the parameter explicitly, or accepts **kwargs.
        Anything else would raise TypeError if we passed it, so we do not.
        """
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            return False
        for param in sig.parameters.values():
            if param.kind is inspect.Parameter.VAR_KEYWORD:
                return True
            if param.name == "idempotency_key" and param.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            ):
                return True
        return False

    @classmethod
    def inject(cls, fn: Callable[..., Any], kwargs: dict, key: str) -> dict:
        """Return kwargs with `idempotency_key` added when the handler can take
        it, and unchanged when it cannot.

        An explicit value already in kwargs always wins: a connector that
        derived a remote-specific key (Stripe's `agent-saga-refund-<charge_id>`,
        which must match what the original request used) knows better than we do.
        """
        if not cls.accepts_key(fn):
            return kwargs
        if kwargs.get("idempotency_key"):
            return kwargs
        merged = dict(kwargs)
        merged["idempotency_key"] = key
        return merged

    # -- execution ledger --------------------------------------------------

    @staticmethod
    def completed_keys(
        journal_path: Optional[str | Path] = None,
        wal_records: Optional[Iterable[dict]] = None,
    ) -> set[str]:
        """Keys already known to have completed.

        Reads both sources on purpose. The daemon's journal records what *it*
        did; the WAL records what the original process did before it died. A
        daemon that consulted only its own journal would happily re-run a
        compensation the crashed process had already finished.
        """
        done: set[str] = set()

        if journal_path is not None:
            path = Path(journal_path)
            if path.exists():
                with open(path, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except (json.JSONDecodeError, ValueError):
                            continue  # a torn final line is not fatal
                        if rec.get("event") in _SUCCESS_EVENTS:
                            token = rec.get("token")
                            if token:
                                done.add(token)

        if wal_records is not None:
            for rec in wal_records:
                if rec.get("event") not in _SUCCESS_EVENTS:
                    continue
                saga_id, step_id = rec.get("saga_id"), rec.get("step_id")
                if saga_id and step_id:
                    done.add(IdempotencyManager.key(saga_id, step_id))
        return done

    @staticmethod
    def attempts(journal_path: Optional[str | Path] = None) -> dict[str, int]:
        """How many times each key has been attempted. Telemetry only -- it is
        deliberately not part of the key. A high count is a signal that a remote
        is flapping, which is worth an alert."""
        counts: dict[str, int] = {}
        if journal_path is None:
            return counts
        path = Path(journal_path)
        if not path.exists():
            return counts
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if rec.get("event") in ("RECOVERY_ATTEMPT", "COMPENSATION_ATTEMPT"):
                    token = rec.get("token")
                    if token:
                        counts[token] = counts.get(token, 0) + 1
        return counts

    @staticmethod
    def should_skip(key: str, completed: set[str], step_id: str) -> bool:
        """Whether to no-op this compensation, logging why when we do."""
        if key in completed:
            logger.info(
                "[IDEMPOTENCY_GUARD] Compensation for step %s already completed. "
                "Skipping.", step_id)
            return True
        return False


__all__ = ["IdempotencyManager", "COMPENSATE_SCOPE"]
