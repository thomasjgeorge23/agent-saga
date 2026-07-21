"""The recovery execution ledger.

This is the record of what a daemon has actually done, and it is the thing that
makes a retry a no-op. It must be visible to *every* daemon that can recover a
given saga -- otherwise the guarantee collapses.

THE BUG THIS EXISTS TO FIX
    Recovery reads a shared WAL (RedisWAL) but used to write its journal to a
    local file and claim with a local file lock. Two nodes sweeping the same
    saga therefore could not see each other: both derived the same idempotency
    key, neither found the other's RECOVERY_SUCCESS, and both ran the
    compensation. Where the remote honours the idempotency key that is merely
    wasteful; where it does not -- any handler without native de-duplication --
    it is a double refund.

    So the ledger is an interface, and it must be as shared as the log. Use
    FileLedger with FileWAL on one host; use a shared ledger whenever the WAL
    itself is shared.

`RedisLedger` is included because a fleet running RedisWAL already has Redis;
it is imported lazily and needs `pip install agent-saga[redis]`. The core stays
dependency-free.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

# Events proving a compensation completed. Shared with idempotency.py so the two
# never drift apart on what "already done" means.
from .idempotency import _SUCCESS_EVENTS, IdempotencyManager

_ATTEMPT_EVENTS = frozenset({"RECOVERY_ATTEMPT", "COMPENSATION_ATTEMPT"})


@runtime_checkable
class RecoveryLedger(Protocol):
    """Append-only record of recovery work, readable by every daemon."""

    async def record(self, event: str, payload: dict) -> None: ...

    async def completed_keys(self) -> set[str]:
        """Idempotency keys already known to have compensated successfully."""
        ...

    async def attempts(self) -> dict[str, int]:
        """Attempts per key. Telemetry: a rising count means a flapping remote."""
        ...


class FileLedger:
    """Local append-only JSONL journal, fsynced. The default.

    Correct for a single host. NOT correct when the WAL is shared across nodes:
    two daemons on different hosts cannot see each other's file, which is
    precisely the double-compensation window described in the module docstring.
    """

    distributed = False

    def __init__(self, path: str | Path, *, daemon_id: str = ""):
        self.path = Path(path)
        self.daemon_id = daemon_id

    async def record(self, event: str, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rec = {"event": event, "ts": time.time(), "daemon_id": self.daemon_id, **payload}
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
            fh.flush()
            # The ledger is the thing that stops a double refund. If it is not
            # durable, a crash re-opens exactly the window it exists to close.
            os.fsync(fh.fileno())

    async def completed_keys(self) -> set[str]:
        return IdempotencyManager.completed_keys(self.path)

    async def attempts(self) -> dict[str, int]:
        return IdempotencyManager.attempts(self.path)


class InMemoryLedger:
    """Process-local ledger for tests and embedded single-process recovery."""

    distributed = False

    def __init__(self, *, daemon_id: str = ""):
        self.daemon_id = daemon_id
        self.records: list[dict] = []

    async def record(self, event: str, payload: dict) -> None:
        self.records.append({"event": event, "ts": time.time(),
                             "daemon_id": self.daemon_id, **payload})

    async def completed_keys(self) -> set[str]:
        return {r["token"] for r in self.records
                if r.get("event") in _SUCCESS_EVENTS and r.get("token")}

    async def attempts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.records:
            if r.get("event") in _ATTEMPT_EVENTS and r.get("token"):
                counts[r["token"]] = counts.get(r["token"], 0) + 1
        return counts


class RedisLedger:
    """Ledger shared across every node in a fleet.

    Pair this with RedisWAL. Two daemons then see each other's successes, so the
    second one skips work the first already finished instead of repeating it.

    Durability caveat is the same as RedisWAL's: Redis acknowledges before
    fsync, so a node failure can lose the last moment of ledger history and
    re-open a small double-compensation window. Run `appendfsync always` if the
    handlers you compensate lack their own idempotency.
    """

    distributed = True

    def __init__(
        self,
        url: str = "redis://localhost:6379/0",
        *,
        key: str = "agent-saga:recovery-ledger",
        client: Any = None,
        daemon_id: str = "",
    ):
        self.url = url
        self.key = key
        self.daemon_id = daemon_id
        self._client = client
        self._owns_client = client is None
        if client is None:
            try:
                import redis.asyncio  # noqa: F401
            except ImportError as exc:
                raise ImportError(
                    "RedisLedger needs the 'redis' package.\n"
                    "    pip install agent-saga[redis]"
                ) from exc

    async def _conn(self):
        if self._client is None:
            from redis.asyncio import Redis

            self._client = Redis.from_url(self.url, decode_responses=True)
        return self._client

    async def record(self, event: str, payload: dict) -> None:
        conn = await self._conn()
        rec = {"event": event, "ts": time.time(), "daemon_id": self.daemon_id, **payload}
        await conn.rpush(self.key, json.dumps(rec, default=str))

    async def _all(self) -> list[dict]:
        conn = await self._conn()
        out = []
        for raw in await conn.lrange(self.key, 0, -1):
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            try:
                out.append(json.loads(raw))
            except (json.JSONDecodeError, ValueError):
                continue
        return out

    async def completed_keys(self) -> set[str]:
        return {r["token"] for r in await self._all()
                if r.get("event") in _SUCCESS_EVENTS and r.get("token")}

    async def attempts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in await self._all():
            if r.get("event") in _ATTEMPT_EVENTS and r.get("token"):
                counts[r["token"]] = counts.get(r["token"], 0) + 1
        return counts

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            close = getattr(self._client, "aclose", None) or getattr(self._client, "close", None)
            if close is not None:
                await close()
        self._client = None


__all__ = ["RecoveryLedger", "FileLedger", "InMemoryLedger", "RedisLedger"]
