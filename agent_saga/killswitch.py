"""Kill switch and quarantine: stop agents doing things, right now.

The first question in an incident is "how do I make it stop", and every other
control here answers a different one. Limits cap a rate. The gate refuses a
category. Neither of them helps at 03:00 when an agent is doing something nobody
predicted and the answer needs to be *everything, now, everywhere*.

Four levers, because "stop" is not one thing:

  * **HALT** -- refuse new side effects immediately. In-flight steps that have
    already left are gone; nothing new leaves.
  * **DRAIN** -- let sagas already running finish, start no new ones. The
    graceful version, for a deploy or a suspected-but-unconfirmed problem.
  * **SCOPE** -- halt one tool, one tag, one tenant. "Stop all wire transfers"
    is a very different blast radius from "stop everything", and an operator who
    can only do the second will hesitate to do either.
  * **QUARANTINE** -- freeze one saga for investigation. Explicitly *not* a
    rollback: during an incident, automatically reversing a hundred sagas can be
    far worse than leaving them still. A quarantined saga makes no further
    calls, is skipped by the recovery daemon, and waits for a human.

THE FAIL-OPEN / FAIL-CLOSED DECISION, which is deliberately different here.
Everywhere else in this library an unreachable backend refuses: a limiter that
cannot check a budget must not authorize spending. Applying that rule to the
kill switch would make it the single largest availability risk in the system --
a Redis blip would halt every agent everywhere, and the control installed to
contain an incident would be causing one. Failing open instead means anyone able
to take the store down can bypass the switch.

So neither: the last known state is cached locally and honoured for a bounded
grace window. A blip is survived; an outage is not a bypass, because once the
grace expires the switch fails closed. The window is the knob, and it is stated
in the constructor rather than hidden, because it is exactly the tradeoff an
operator must own.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

logger = logging.getLogger("agent_saga.killswitch")

RUNNING = "RUNNING"
HALTED = "HALTED"
DRAINING = "DRAINING"

DEFAULT_GRACE = 30.0
DEFAULT_CACHE_TTL = 1.0


class Halted(Exception):
    """Raised before any side effect. Nothing has happened."""

    def __init__(self, state: str, scope: str, reason: str, by: str = ""):
        self.state, self.scope, self.reason, self.by = state, scope, reason, by
        who = f" by {by}" if by else ""
        super().__init__(
            f"[{state}] {scope}: {reason}{who}. Lift it with: agent-saga resume"
            + (f" --scope {scope}" if scope != "*" else ""))


@dataclass
class Switch:
    """One halt, over one scope."""

    scope: str = "*"
    state: str = HALTED
    reason: str = ""
    by: str = ""
    at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    """Optional auto-expiry. A halt someone forgot to lift is its own outage;
    a deploy-time drain that lifts itself is usually what was wanted."""

    def active(self, now: Optional[float] = None) -> bool:
        now = now or time.time()
        return self.state != RUNNING and (not self.expires_at or now < self.expires_at)

    def to_dict(self) -> dict:
        return {"scope": self.scope, "state": self.state, "reason": self.reason,
                "by": self.by, "at": self.at, "expires_at": self.expires_at}

    @classmethod
    def from_dict(cls, data: dict) -> "Switch":
        return cls(**{k: v for k, v in data.items()
                      if k in cls.__dataclass_fields__})

    def summary(self) -> str:
        age = int(time.time() - self.at)
        left = (f", expires in {int(self.expires_at - time.time())}s"
                if self.expires_at else "")
        return (f"{self.state} scope={self.scope} by={self.by or '-'} "
                f"({age}s ago{left}): {self.reason}")


# ---------------------------------------------------------------------------
# Stores
# ---------------------------------------------------------------------------

@runtime_checkable
class SwitchStore(Protocol):
    distributed: bool

    def read(self) -> dict:
        """Every active switch, keyed by scope. Raises if unreachable."""
        ...

    def write(self, switch: Switch) -> None:
        ...

    def clear(self, scope: str) -> bool:
        ...

    def quarantine(self, saga_id: str, reason: str, by: str) -> None:
        ...

    def quarantined(self) -> dict:
        ...

    def release(self, saga_id: str) -> bool:
        ...


class FileSwitchStore:
    """A single JSON file. Zero infrastructure, and an operator can `cat` it.

    Correct for one host. On a fleet each node reads its own copy, so a halt has
    to be distributed some other way -- which is why `distributed` is False and
    the gate says so loudly at startup.
    """

    distributed = False

    def __init__(self, path: str | Path = "./.agent-saga-switch.json"):
        self.path = Path(path)

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return {"switches": {}, "quarantined": {}}
        except (json.JSONDecodeError, ValueError) as exc:
            # A corrupt switch file must not read as "nothing is halted".
            raise RuntimeError(f"switch file {self.path} is unreadable: {exc}") from exc

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    def read(self) -> dict:
        return {k: Switch.from_dict(v)
                for k, v in (self._load().get("switches") or {}).items()}

    def write(self, switch: Switch) -> None:
        data = self._load()
        data.setdefault("switches", {})[switch.scope] = switch.to_dict()
        self._save(data)

    def clear(self, scope: str) -> bool:
        data = self._load()
        removed = (data.get("switches") or {}).pop(scope, None) is not None
        if removed:
            self._save(data)
        return removed

    def quarantine(self, saga_id: str, reason: str, by: str) -> None:
        data = self._load()
        data.setdefault("quarantined", {})[saga_id] = {
            "reason": reason, "by": by, "at": time.time()}
        self._save(data)

    def quarantined(self) -> dict:
        return self._load().get("quarantined") or {}

    def release(self, saga_id: str) -> bool:
        data = self._load()
        removed = (data.get("quarantined") or {}).pop(saga_id, None) is not None
        if removed:
            self._save(data)
        return removed


class RedisSwitchStore:
    """One switch for the whole fleet.

    A kill switch that only stops the pod you happen to be on is not a kill
    switch. This is the only correct backend for more than one process, and the
    one an incident runbook should name.

    Requires `pip install agent-saga[redis]`.
    """

    distributed = True

    def __init__(self, url: str = "redis://localhost:6379/0", *,
                 client: Any = None, key_prefix: str = "agent-saga:switch:"):
        self.url = url
        self.key_prefix = key_prefix
        self._client = client
        if client is None:
            try:
                import redis  # noqa: F401
            except ImportError as exc:
                raise ImportError(
                    "RedisSwitchStore needs the 'redis' package.\n"
                    "    pip install agent-saga[redis]") from exc

    def _conn(self) -> Any:
        if self._client is None:
            import redis

            self._client = redis.Redis.from_url(self.url, decode_responses=True)
        return self._client

    def read(self) -> dict:
        raw = self._conn().hgetall(f"{self.key_prefix}switches") or {}
        out = {}
        for scope, blob in raw.items():
            try:
                out[scope] = Switch.from_dict(json.loads(blob))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        return out

    def write(self, switch: Switch) -> None:
        self._conn().hset(f"{self.key_prefix}switches", switch.scope,
                          json.dumps(switch.to_dict()))

    def clear(self, scope: str) -> bool:
        return bool(self._conn().hdel(f"{self.key_prefix}switches", scope))

    def quarantine(self, saga_id: str, reason: str, by: str) -> None:
        self._conn().hset(f"{self.key_prefix}quarantine", saga_id,
                          json.dumps({"reason": reason, "by": by, "at": time.time()}))

    def quarantined(self) -> dict:
        raw = self._conn().hgetall(f"{self.key_prefix}quarantine") or {}
        out = {}
        for saga_id, blob in raw.items():
            try:
                out[saga_id] = json.loads(blob)
            except (json.JSONDecodeError, TypeError, ValueError):
                out[saga_id] = {"reason": "?", "by": "?"}
        return out

    def release(self, saga_id: str) -> bool:
        return bool(self._conn().hdel(f"{self.key_prefix}quarantine", saga_id))


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------

class KillSwitch:
    """Consulted before anything else the gate does.

    Ordering matters: a halted system must not spend budget deciding to refuse,
    and must not wake a human to approve a call it will refuse anyway.
    """

    def __init__(self, store: Optional[Any] = None, *,
                 grace: float = DEFAULT_GRACE,
                 cache_ttl: float = DEFAULT_CACHE_TTL,
                 wal: Any = None):
        self.store = store or FileSwitchStore()
        self.grace = grace
        """How long a cached state is honoured once the store is unreachable.
        After it, the switch fails closed. Zero means fail closed immediately --
        maximum safety, and a store outage becomes a fleet outage."""

        self.cache_ttl = cache_ttl
        """How stale a *healthy* read may be. This is on the hot path of every
        tool call, so a network round trip per call is not affordable; a second
        of staleness on a halt is the price, and it is short enough that "stop
        everything" still means seconds, not minutes."""

        self.wal = wal
        self._cache: dict = {}
        self._quarantine: dict = {}
        self._read_at: float = 0.0
        self._degraded_since: float = 0.0

    # -- state -------------------------------------------------------------

    def _refresh(self) -> None:
        now = time.time()
        if self._read_at and now - self._read_at < self.cache_ttl:
            return
        try:
            self._cache = self.store.read()
            self._quarantine = self.store.quarantined()
            self._read_at = now
            if self._degraded_since:
                logger.info("kill switch store reachable again")
                self._degraded_since = 0.0
        except Exception as exc:
            if not self._degraded_since:
                self._degraded_since = now
                logger.error(
                    "kill switch store unreachable (%r). Honouring the last known "
                    "state for %.0fs, then refusing everything.", exc, self.grace)
            # >= so that grace=0 means "fail closed immediately". With > , the
            # first failed read is always inside the window (elapsed is exactly
            # zero), and a deployment asking for maximum safety would silently
            # get one free pass on every outage.
            if now - self._degraded_since >= self.grace:
                raise Halted(
                    HALTED, "*",
                    f"kill switch state has been unreadable for "
                    f"{int(now - self._degraded_since)}s ({exc!r}), so whether "
                    f"this system is halted cannot be established") from exc

    def scopes_for(self, tool: str = "", tags: Any = ()) -> list:
        """Which scopes could halt this call. `*` always applies."""
        out = ["*"]
        if tool:
            out.append(f"tool:{tool}")
            if "." in tool or "__" in tool:
                prefix = tool.split("__")[0].split(".")[0]
                out.append(f"tool:{prefix}.*")
        out.extend(f"tag:{t}" for t in (tags or ()))
        return out

    def active(self, tool: str = "", tags: Any = ()) -> Optional[Switch]:
        self._refresh()
        now = time.time()
        for scope in self.scopes_for(tool, tags):
            switch = self._cache.get(scope)
            if switch is not None and switch.active(now):
                return switch
        return None

    # -- checks ------------------------------------------------------------

    def check_step(self, tool: str = "", tags: Any = (),
                   saga_id: str = "") -> None:
        """Refuse a new side effect if anything covering it is halted.

        DRAINING deliberately does *not* stop a step: a draining system is
        letting in-flight sagas finish, and blocking their remaining steps would
        strand every one of them half-done -- the opposite of draining.
        """
        if saga_id and self.is_quarantined(saga_id):
            info = self._quarantine.get(saga_id, {})
            raise Halted("QUARANTINED", f"saga:{saga_id}",
                         info.get("reason", "under investigation"),
                         info.get("by", ""))
        switch = self.active(tool, tags)
        if switch is not None and switch.state == HALTED:
            raise Halted(switch.state, switch.scope, switch.reason, switch.by)

    def check_start(self, tags: Any = ()) -> None:
        """Refuse to *begin* a saga. Both HALTED and DRAINING stop this."""
        switch = self.active("", tags)
        if switch is not None:
            raise Halted(switch.state, switch.scope,
                         switch.reason or "system is draining", switch.by)

    def is_quarantined(self, saga_id: str) -> bool:
        try:
            self._refresh()
        except Halted:
            # Cannot establish state. Treat as quarantined: the daemon must not
            # compensate a saga it cannot prove is free to touch.
            return True
        return saga_id in self._quarantine

    # -- operations --------------------------------------------------------

    def halt(self, *, scope: str = "*", reason: str, by: str,
             ttl: float = 0.0, drain: bool = False) -> Switch:
        switch = Switch(scope=scope, state=DRAINING if drain else HALTED,
                        reason=reason, by=by,
                        expires_at=time.time() + ttl if ttl else 0.0)
        self.store.write(switch)
        self._read_at = 0.0                     # next check sees it immediately
        logger.error("%s %s by %s: %s", switch.state, scope, by, reason)
        self._record("KILLSWITCH_ENGAGED", switch.to_dict())
        return switch

    def resume(self, *, scope: str = "*", by: str = "") -> bool:
        lifted = self.store.clear(scope)
        self._read_at = 0.0
        if lifted:
            logger.warning("resumed %s by %s", scope, by or "-")
            self._record("KILLSWITCH_RELEASED", {"scope": scope, "by": by})
        return lifted

    def quarantine(self, saga_id: str, *, reason: str, by: str) -> None:
        """Freeze one saga. Not a rollback, on purpose.

        Automatically reversing sagas during an incident can be much worse than
        leaving them still -- a hundred unplanned refunds is its own outage. The
        saga stops, the daemon leaves it alone, and a human decides.
        """
        self.store.quarantine(saga_id, reason, by)
        self._read_at = 0.0
        logger.error("QUARANTINED saga %s by %s: %s", saga_id, by, reason)
        self._record("SAGA_QUARANTINED",
                     {"saga_id": saga_id, "reason": reason, "by": by})

    def release(self, saga_id: str, *, by: str = "") -> bool:
        released = self.store.release(saga_id)
        self._read_at = 0.0
        if released:
            self._record("SAGA_RELEASED", {"saga_id": saga_id, "by": by})
        return released

    def status(self) -> dict:
        try:
            self._refresh()
            reachable = True
        except Halted:
            reachable = False
        return {
            "store": type(self.store).__name__,
            "distributed": getattr(self.store, "distributed", False),
            "reachable": reachable,
            "degraded_for": (round(time.time() - self._degraded_since, 1)
                             if self._degraded_since else 0),
            "switches": [s.summary() for s in self._cache.values() if s.active()],
            "quarantined": dict(self._quarantine),
        }

    def _record(self, event: str, payload: dict) -> None:
        if self.wal is None:
            return
        try:
            self.wal.append(event, payload)
        except Exception as exc:
            logger.error("could not record %s: %r", event, exc)


_SWITCH: Optional[KillSwitch] = None


def get_kill_switch() -> Optional[KillSwitch]:
    return _SWITCH


def set_kill_switch(switch: Optional[KillSwitch]) -> None:
    """Install a process-wide switch. The gate consults it before every step."""
    global _SWITCH
    _SWITCH = switch
    if switch is not None and not getattr(switch.store, "distributed", False):
        logger.warning(
            "%s is not distributed: a halt reaches only this process. On more "
            "than one node use RedisSwitchStore, or an operator will believe "
            "they stopped a fleet they did not.",
            type(switch.store).__name__)


__all__ = [
    "KillSwitch", "Halted", "Switch", "SwitchStore",
    "FileSwitchStore", "RedisSwitchStore",
    "get_kill_switch", "set_kill_switch",
    "RUNNING", "HALTED", "DRAINING",
]
