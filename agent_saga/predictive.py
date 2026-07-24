"""Predictive intent pre-execution.

Perceived latency is dominated by work that could have started while the user
was still typing. This runs the *safe* part of a saga ahead of the ask -- the
lookups and reservations -- so that when intent is confirmed the answer is
already in hand.

The reason this is normally a terrible idea is that speculating on the wrong
step performs a real side effect for an intent the user never confirmed. So the
rule here is absolute and enforced, not documented:

    **Only REVERSIBLE steps may be speculated.**

An attempt to speculate a COMPENSABLE or IRREVERSIBLE step raises
:class:`SpeculationRefused`. You cannot pre-authorise a card while someone is
still typing, and no configuration flag will let you.

Three further properties keep a speculation from becoming a liability:

* **TTL lease.** Every speculation carries an expiry. Past it the result is
  never served, so an abandoned draft cannot be redeemed an hour later.
* **Intent binding.** The result is bound to a hash of the intent that produced
  it. Change one character of the request and the cached answer no longer
  matches -- you can never be served a confident answer to a different question.
* **Authenticated lease.** The lease is an HMAC over (intent, tool, expiry). A
  speculation cannot be forged or replayed by anything that can inject text,
  which is the attack that matters when the caller is an LLM.

    px = PredictiveExecutor(ttl=60)
    await px.speculate("inventory.check", check_stock, intent=draft)   # user typing
    ...
    hit = px.confirm(final_text)     # same intent -> instant, no re-run
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .semantics import ActionSemantics

logger = logging.getLogger("agent_saga.predictive")

DEFAULT_TTL = 60.0


class SpeculationRefused(RuntimeError):
    """Raised when a caller tries to speculate a step that could have effects."""


def intent_hash(intent: Any) -> str:
    """Stable fingerprint of an intent. Whitespace-insensitive at the edges so a
    trailing space while typing does not invalidate an otherwise identical ask."""
    text = intent if isinstance(intent, str) else repr(intent)
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


@dataclass
class Speculation:
    intent_hash: str
    tool: str
    result: Any
    created_at: float
    expires_at: float
    lease: str
    consumed: bool = False
    error: Optional[BaseException] = None

    def expired(self, now: Optional[float] = None) -> bool:
        return (now if now is not None else time.time()) >= self.expires_at

    def usable(self, now: Optional[float] = None) -> bool:
        return not self.consumed and self.error is None and not self.expired(now)


@dataclass
class PredictiveStats:
    speculated: int = 0
    hits: int = 0
    misses: int = 0
    expired: int = 0
    refused: int = 0
    cancelled: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return (self.hits / total) if total else 0.0

    def summary(self) -> str:
        return (f"predictive: {self.speculated} speculated, {self.hits} hit, "
                f"{self.misses} miss ({self.hit_rate:.0%}), {self.expired} expired, "
                f"{self.refused} refused")


class PredictiveExecutor:
    """Runs REVERSIBLE steps ahead of confirmed intent, under a TTL lease."""

    def __init__(self, *, ttl: float = DEFAULT_TTL, secret: Optional[bytes] = None,
                 max_entries: int = 256, clock: Callable[[], float] = time.time):
        if ttl <= 0:
            raise ValueError("ttl must be > 0")
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self.ttl = ttl
        # Per-process secret: a lease is only meaningful to the process that
        # issued it, so a speculation cannot be minted elsewhere and presented
        # here as if it had already run.
        self._secret = secret or os.urandom(32)
        self.max_entries = max_entries
        self._clock = clock
        self._entries: dict[str, Speculation] = {}
        self.stats = PredictiveStats()

    # -- lease -------------------------------------------------------------

    def _mint_lease(self, ih: str, tool: str, expires_at: float) -> str:
        msg = f"{ih}|{tool}|{expires_at:.6f}".encode("utf-8")
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()

    def _lease_valid(self, spec: Speculation) -> bool:
        expected = self._mint_lease(spec.intent_hash, spec.tool, spec.expires_at)
        return hmac.compare_digest(expected, spec.lease)

    # -- speculation -------------------------------------------------------

    async def speculate(self, tool: str, forward: Callable[[], Any], *, intent: Any,
                        semantics: ActionSemantics = ActionSemantics.REVERSIBLE,
                        ttl: Optional[float] = None) -> Speculation:
        """Run `forward` now, against an intent that is not yet confirmed.

        Refuses anything but REVERSIBLE: a speculated effect that cannot be
        un-done is just an effect the user never asked for."""
        if semantics is not ActionSemantics.REVERSIBLE:
            self.stats.refused += 1
            raise SpeculationRefused(
                f"refusing to speculate {tool!r} declared {semantics.name}: only "
                f"REVERSIBLE steps may run before intent is confirmed. A "
                f"{semantics.name} step performs an effect the user has not asked "
                f"for yet.")

        now = self._clock()
        ih = intent_hash(intent)
        expires_at = now + (ttl if ttl is not None else self.ttl)
        lease = self._mint_lease(ih, tool, expires_at)

        result: Any = None
        error: Optional[BaseException] = None
        try:
            out = forward()
            result = await out if inspect.isawaitable(out) else out
        except Exception as exc:          # a speculative failure is not a saga failure
            error = exc
            logger.debug("speculation for %s failed (discarded): %r", tool, exc)

        spec = Speculation(intent_hash=ih, tool=tool, result=result, created_at=now,
                           expires_at=expires_at, lease=lease, error=error)
        self._entries[ih] = spec
        self.stats.speculated += 1
        self._evict(now)
        return spec

    def confirm(self, intent: Any, *, tool: Optional[str] = None) -> Optional[Speculation]:
        """Redeem a speculation for a now-confirmed intent.

        Returns the speculation on a hit -- its `.result` is ready, no re-run --
        or None, in which case the caller simply executes normally. A hit is
        single-use: the entry is consumed so a stale answer cannot be served
        twice."""
        now = self._clock()
        ih = intent_hash(intent)
        spec = self._entries.get(ih)
        if spec is None:
            self.stats.misses += 1
            return None
        if spec.expired(now):
            self._entries.pop(ih, None)
            self.stats.expired += 1
            self.stats.misses += 1
            return None
        if tool is not None and spec.tool != tool:
            self.stats.misses += 1
            return None
        if not self._lease_valid(spec):
            # Tampered or forged: drop it and fall back to real execution.
            logger.warning("speculation for %s failed lease validation; discarding",
                           spec.tool)
            self._entries.pop(ih, None)
            self.stats.misses += 1
            return None
        if not spec.usable(now):
            self.stats.misses += 1
            return None

        spec.consumed = True
        self._entries.pop(ih, None)
        self.stats.hits += 1
        return spec

    def cancel(self, intent: Any) -> bool:
        """Abandon a speculation -- the user changed or dropped the intent."""
        removed = self._entries.pop(intent_hash(intent), None) is not None
        if removed:
            self.stats.cancelled += 1
        return removed

    def sweep(self, now: Optional[float] = None) -> int:
        """Drop expired speculations. Safe to call on a timer."""
        now = now if now is not None else self._clock()
        dead = [k for k, s in self._entries.items() if s.expired(now)]
        for k in dead:
            self._entries.pop(k, None)
        self.stats.expired += len(dead)
        return len(dead)

    def _evict(self, now: float) -> None:
        """Keep memory bounded: expired first, then oldest."""
        self.sweep(now)
        while len(self._entries) > self.max_entries:
            oldest = min(self._entries.items(), key=lambda kv: kv[1].created_at)[0]
            self._entries.pop(oldest, None)

    @property
    def pending(self) -> int:
        return len(self._entries)


__all__ = [
    "PredictiveExecutor", "Speculation", "SpeculationRefused", "PredictiveStats",
    "intent_hash", "DEFAULT_TTL",
]
