"""Circuit breaker: stop hammering a dependency that is already failing.

The limits in `limits.py` cap what an agent is *allowed* to do. This caps what
is *worth* doing: when Stripe has been timing out for ninety seconds, the
hundredth charge attempt will not succeed either, and every one of them costs a
saga, a WAL fsync, a compensation of unknown outcome, and thirty seconds of an
agent's life. Refusing quickly is the kinder answer to everyone including the
dependency.

Unlike every other control here it needs *outcome* feedback, which is why it
could not be built alongside the budgets. A limit decides before the call and
never learns what happened; a breaker is nothing but what happened. That is a
different lifecycle, and bolting it onto the reserve-on-authorize model would
have compromised both.

Three decisions specific to this codebase, each with a bad obvious answer:

  * **A refusal is not a failure.** A `PreFlightViolation` -- over budget,
    blocked by policy, denied by a human -- means the system worked. Counting
    those would trip the breaker precisely when the controls were doing their
    job, and the breaker would then block the calls that were still fine.
    Only a tool that actually ran and raised counts, plus a timeout, which is
    the classic breaker signal and here also the most dangerous outcome
    (`STEP_UNKNOWN`).

  * **A breaker must never block a rollback.** If the forward path is failing,
    the compensations are exactly what you need to run. Blocking them because
    their connector looks sick would strand money mid-transaction, converting a
    dependency outage into a financial one. Compensations bypass the gate
    entirely, so this is a property of where the check sits -- stated here
    because it is load-bearing and easy to break later.

  * **This one fails OPEN, and that is not an inconsistency.** A budget that
    cannot be verified must refuse, because failing open means overspending. A
    breaker is an *availability* protection, not a safety one: if its store is
    unreachable, refusing all work would mean an outage in the breaker's own
    infrastructure taking down agents that had nothing wrong with them. So it
    falls back to per-process state and keeps going. The rule is to fail toward
    the behaviour you would have had without the feature -- for a budget that
    is "refuse", for a breaker it is "make the call".
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable

logger = logging.getLogger("agent_saga.breaker")

CLOSED = "CLOSED"
OPEN = "OPEN"
HALF_OPEN = "HALF_OPEN"


class CircuitOpen(Exception):
    """Raised before the call. The dependency is already known to be failing."""

    def __init__(self, key: str, state: str, failures: int, retry_in: float,
                 detail: str = ""):
        self.key, self.state, self.failures, self.retry_in = key, state, failures, retry_in
        super().__init__(
            f"circuit {state} for {key!r} after {failures} recent failure(s); "
            f"retrying in {retry_in:.0f}s"
            + (f". {detail}" if detail else ""))


@dataclass(frozen=True)
class BreakerPolicy:
    failure_threshold: int = 5
    """Consecutive failures, or failures within `window`, before opening."""

    window: float = 60.0
    min_volume: int = 5
    """Below this many calls in the window, a failure *rate* means nothing.
    Two failures out of two is 100% and is not evidence of anything."""

    failure_rate: float = 0.5
    cool_down: float = 30.0
    """How long to stay OPEN before letting a probe through."""

    half_open_probes: int = 1
    """Concurrent trial calls allowed while probing. More than one turns
    recovery into a small thundering herd against something still fragile."""


@dataclass
class CircuitState:
    key: str
    state: str = CLOSED
    consecutive_failures: int = 0
    opened_at: float = 0.0
    probes_in_flight: int = 0
    events: list = field(default_factory=list)   # (timestamp, ok)
    tripped_count: int = 0

    def prune(self, now: float, window: float) -> None:
        cutoff = now - window
        self.events = [e for e in self.events if e[0] > cutoff]

    def rate(self) -> tuple:
        failures = sum(1 for _, ok in self.events if not ok)
        return failures, len(self.events)


@runtime_checkable
class BreakerStore(Protocol):
    distributed: bool

    def get(self, key: str) -> CircuitState:
        ...

    def put(self, state: CircuitState) -> None:
        ...

    def all(self) -> dict:
        ...


class InProcessBreakerStore:
    """Per-process state.

    Weaker than a shared store and not dangerous the way a per-process *budget*
    is: each node independently notices the dependency is sick and independently
    protects itself. It converges on the right answer more slowly, rather than
    silently multiplying an allowance.
    """

    distributed = False

    def __init__(self) -> None:
        self._states: dict = {}
        self._mutex = threading.Lock()

    def get(self, key: str) -> CircuitState:
        with self._mutex:
            state = self._states.get(key)
            if state is None:
                state = CircuitState(key=key)
                self._states[key] = state
            return state

    def put(self, state: CircuitState) -> None:
        with self._mutex:
            self._states[state.key] = state

    def all(self) -> dict:
        with self._mutex:
            return dict(self._states)

    def reset(self) -> None:
        with self._mutex:
            self._states.clear()


class CircuitBreaker:
    """One breaker per tool, consulted before the call and told the outcome."""

    def __init__(self, policy: Optional[BreakerPolicy] = None, *,
                 store: Optional[Any] = None, wal: Any = None):
        self.policy = policy or BreakerPolicy()
        self.store = store or InProcessBreakerStore()
        self.wal = wal
        self._local = InProcessBreakerStore()
        """Fallback when a shared store is unreachable. Degrading to
        per-process protection beats degrading to none."""

    def _state(self, key: str) -> tuple:
        try:
            return self.store.get(key), False
        except Exception as exc:
            logger.warning("breaker store unreachable (%r); using local state", exc)
            return self._local.get(key), True

    def _save(self, state: CircuitState, degraded: bool) -> None:
        target = self._local if degraded else self.store
        try:
            target.put(state)
        except Exception as exc:
            logger.warning("could not persist breaker state: %r", exc)

    # -- the check ---------------------------------------------------------

    def check(self, key: str) -> None:
        """Refuse if the circuit is open. Called before the tool runs.

        Never called on the rollback path: a breaker that blocked compensations
        would strand money mid-transaction the moment a connector got sick.
        """
        state, degraded = self._state(key)
        now = time.time()

        if state.state == OPEN:
            elapsed = now - state.opened_at
            if elapsed < self.policy.cool_down:
                raise CircuitOpen(key, OPEN, state.consecutive_failures,
                                  self.policy.cool_down - elapsed)
            # Cool-down done: try one call and see.
            state.state = HALF_OPEN
            state.probes_in_flight = 0
            logger.info("circuit for %r is half-open; probing", key)
            self._record("CIRCUIT_HALF_OPEN", {"key": key})

        if state.state == HALF_OPEN:
            if state.probes_in_flight >= self.policy.half_open_probes:
                raise CircuitOpen(
                    key, HALF_OPEN, state.consecutive_failures,
                    self.policy.cool_down,
                    "a trial call is already in flight")
            state.probes_in_flight += 1

        self._save(state, degraded)

    # -- outcome feedback --------------------------------------------------

    def record_success(self, key: str) -> None:
        state, degraded = self._state(key)
        now = time.time()
        state.events.append((now, True))
        state.prune(now, self.policy.window)
        state.consecutive_failures = 0
        if state.state == HALF_OPEN:
            state.probes_in_flight = max(0, state.probes_in_flight - 1)
            state.state = CLOSED
            state.events = []       # a fresh start, not a slow climb out
            logger.warning("circuit for %r closed after a successful probe", key)
            self._record("CIRCUIT_CLOSED", {"key": key})
        self._save(state, degraded)

    def record_failure(self, key: str, error: str = "") -> None:
        state, degraded = self._state(key)
        now = time.time()
        state.events.append((now, False))
        state.prune(now, self.policy.window)
        state.consecutive_failures += 1

        if state.state == HALF_OPEN:
            # The probe failed. Straight back to open with a fresh cool-down;
            # letting the next caller probe immediately would hammer something
            # that has just told us it is still broken.
            state.probes_in_flight = max(0, state.probes_in_flight - 1)
            self._open(state, "probe failed", error)
        elif self._should_trip(state):
            self._open(state, "threshold reached", error)
        self._save(state, degraded)

    def _should_trip(self, state: CircuitState) -> bool:
        if state.state != CLOSED:
            return False
        if state.consecutive_failures >= self.policy.failure_threshold:
            return True
        failures, total = state.rate()
        # A rate needs volume behind it: two failures out of two is 100% and
        # evidence of nothing.
        return (total >= self.policy.min_volume
                and failures / total >= self.policy.failure_rate)

    def _open(self, state: CircuitState, reason: str, error: str) -> None:
        state.state = OPEN
        state.opened_at = time.time()
        state.tripped_count += 1
        failures, total = state.rate()
        logger.error("circuit OPEN for %r (%s): %d/%d recent calls failed. %s",
                     state.key, reason, failures, total, error[:200])
        self._record("CIRCUIT_OPEN", {
            "key": state.key, "reason": reason, "failures": failures,
            "calls": total, "consecutive": state.consecutive_failures,
            "cool_down": self.policy.cool_down, "error": error[:500]})

    # -- operations --------------------------------------------------------

    def reset(self, key: str) -> None:
        """Force a circuit closed. For an operator who knows the dependency is
        healthy again and does not want to wait out the cool-down."""
        state, degraded = self._state(key)
        state.state = CLOSED
        state.consecutive_failures = 0
        state.probes_in_flight = 0
        state.events = []
        self._save(state, degraded)
        self._record("CIRCUIT_RESET", {"key": key})

    def status(self) -> dict:
        try:
            states = self.store.all()
        except Exception:
            states = self._local.all()
        return {k: {"state": s.state, "consecutive_failures": s.consecutive_failures,
                    "tripped": s.tripped_count}
                for k, s in states.items()}

    def _record(self, event: str, payload: dict) -> None:
        if self.wal is None:
            return
        try:
            self.wal.append(event, payload)
        except Exception as exc:
            logger.error("could not record %s: %r", event, exc)


_BREAKER: Optional[CircuitBreaker] = None


def get_breaker() -> Optional[CircuitBreaker]:
    return _BREAKER


def set_breaker(breaker: Optional[CircuitBreaker]) -> None:
    """Install a process-wide breaker. The gate consults it before each step and
    the saga context reports every outcome back to it."""
    global _BREAKER
    _BREAKER = breaker


__all__ = ["CircuitBreaker", "BreakerPolicy", "CircuitOpen", "CircuitState",
           "BreakerStore", "InProcessBreakerStore",
           "get_breaker", "set_breaker", "CLOSED", "OPEN", "HALF_OPEN"]
