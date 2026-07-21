"""Append-only write-ahead log with tiered durability.

The honest latency story:

  * append()  -- synchronous, in-process, lock-free deque push. Sub-microsecond.
                 Survives a caught exception. Does NOT survive SIGKILL.
  * barrier() -- awaits flush + fsync. Costs a real disk round trip (~0.1-5ms
                 depending on hardware). Survives SIGKILL.

We pay for the barrier only where losing the record is unacceptable: before
any COMPENSABLE or IRREVERSIBLE effect. REVERSIBLE steps ride the fast path.
That tiering is why the p50 overhead number is defensible; a WAL that fsyncs
every event would not be.
"""

from __future__ import annotations

import asyncio
import enum
import json
import os
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional


class BackpressurePolicy(enum.Enum):
    """What append() does when the in-memory buffer is full.

    A silently dropped record is a silently unrecoverable side effect: if the
    STEP_INTENT for a charge never lands, the recovery daemon has no way to undo
    it. So the default is RAISE -- fail the step *before* its effect runs, rather
    than let the agent proceed with an incomplete rollback history.
    """

    RAISE = "RAISE"
    """Raise WALBackpressure on overflow. The intent append happens before the
    side effect, so the step aborts cleanly with nothing executed. Safe default."""

    BLOCK = "BLOCK"
    """Never drop. append() lets the buffer grow; the async durable path yields
    via ensure_capacity() so the flush loop can drain. For high-integrity work
    that would rather slow down than lose a record."""

    DROP_SILENT = "DROP_SILENT"
    """Count the drop and return a sentinel (-1), never fooling barrier() into
    reporting a dropped record as durable. Only for low-value REVERSIBLE work
    where a missing WAL line costs nothing (the state dies with the process)."""


class WALBackpressure(RuntimeError):
    """The WAL buffer is full and the policy is RAISE. Raised before any side
    effect, so the transaction is safely abortable."""


DROPPED = -1
"""append() sentinel: the record was NOT written. Never a valid seq."""

_UNSET = object()
"""Distinguishes 'no encryptor argument given' (resolve from env) from an
explicit None (force plaintext)."""


class AsyncWAL:
    def __init__(
        self,
        path: Optional[str | Path] = None,
        *,
        max_buffer: int = 100_000,
        backpressure: BackpressurePolicy = BackpressurePolicy.RAISE,
        encryptor: Any = _UNSET,
    ):
        self.path = Path(path) if path else None
        # _UNSET -> resolve from the environment/module at start(); an explicit
        # value (including None) overrides. None means plaintext even if a key
        # is set in the environment.
        self._encryptor_arg = encryptor
        self._encryptor = None
        self._buf: deque = deque()
        self._seq = 0
        self._durable_seq = 0
        self._waiters: list[tuple[int, asyncio.Future]] = []
        self._wake: Optional[asyncio.Event] = None
        self._task: Optional[asyncio.Task] = None
        self._closing = False
        self._fh = None
        self._max_buffer = max_buffer
        self.backpressure = backpressure
        self.dropped = 0
        """Count of records shed under DROP_SILENT. Non-zero means rollback
        history is incomplete -- surface it, never hide it."""
        self.barriers = 0
        """How many callers actually blocked waiting for durability."""
        self.flush_cycles = 0
        """How many fsyncs we actually performed. barriers/flush_cycles is the
        group-commit amortization factor -- the number that explains why the
        durable path scales with concurrency instead of collapsing."""

    async def start(self) -> None:
        from .encryption import get_wal_encryptor

        self._encryptor = (get_wal_encryptor() if self._encryptor_arg is _UNSET
                           else self._encryptor_arg)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.path, "a", encoding="utf-8")
        self._wake = asyncio.Event()
        self._task = asyncio.create_task(self._flush_loop())

    def append(self, event: str, payload: dict) -> int:
        """Hot path. Synchronous by design -- making this `async` would put an
        event-loop scheduling hop between the agent and every tool call.

        Returns the record's seq, or DROPPED (-1) under DROP_SILENT on overflow.
        Raises WALBackpressure under RAISE. Under BLOCK, never drops (the buffer
        may exceed max_buffer transiently; ensure_capacity() bounds it)."""
        if len(self._buf) >= self._max_buffer:
            if self.backpressure is BackpressurePolicy.RAISE:
                raise WALBackpressure(
                    f"WAL buffer full ({self._max_buffer} records); the flush loop "
                    f"is not draining fast enough. Refusing to proceed without a "
                    f"durable record. (event={event!r})"
                )
            if self.backpressure is BackpressurePolicy.DROP_SILENT:
                self.dropped += 1
                return DROPPED
            # BLOCK: fall through and append anyway -- never lose a record.
        self._seq += 1
        self._buf.append({"seq": self._seq, "event": event, "ts": time.time(), **payload})
        if self._wake is not None and not self._wake.is_set():
            self._wake.set()
        return self._seq

    async def ensure_capacity(self) -> None:
        """Under BLOCK, yield until the buffer has room so the next append does
        not push it further past the limit. A no-op under other policies. Bounded
        by flush speed, not a busy-spin: each iteration wakes the flusher and
        awaits a real scheduling turn."""
        if self.backpressure is not BackpressurePolicy.BLOCK:
            return
        while len(self._buf) >= self._max_buffer and self._task is not None:
            if self._wake is not None:
                self._wake.set()
            await asyncio.sleep(0.001)

    async def barrier(self, seq: Optional[int] = None) -> None:
        """Block until every record up to `seq` is durable on disk."""
        target = self._seq if seq is None else seq
        if self._durable_seq >= target:
            return
        if self._task is None:
            raise RuntimeError("WAL not started; call await wal.start()")
        self.barriers += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters.append((target, fut))
        if self._wake is not None:
            self._wake.set()
        await fut

    async def _flush_loop(self) -> None:
        assert self._wake is not None
        try:
            while True:
                await self._wake.wait()
                self._wake.clear()
                await self._flush_once()
                if self._closing:
                    # Drain anything that arrived while the last flush ran, then
                    # exit cleanly. Because every write above is awaited, no
                    # worker thread is touching the file when this returns --
                    # which is what makes close() safe to shut the handle.
                    await self._flush_once()
                    return
        except asyncio.CancelledError:
            # Process teardown may cancel us. Best-effort synchronous drain so a
            # pending barrier still lands, then propagate.
            self._flush_sync()
            self._resolve_waiters()
            raise

    async def _flush_once(self) -> None:
        # Take everything queued as one batch. Concurrent barriers landing in
        # the same cycle share a single fsync -- group commit, for free, as a
        # consequence of batching.
        batch = self._take()
        if batch:
            if self._fh is not None:
                # fsync is a blocking syscall. Running it off the loop keeps it
                # from stalling every other saga in the process, including
                # REVERSIBLE steps that never asked to pay for disk.
                await asyncio.to_thread(self._write_batch, batch)
            self._durable_seq = batch[-1]["seq"]
        self._resolve_waiters()

    def _flush_sync(self) -> None:
        batch = self._take()
        if batch:
            if self._fh is not None:
                self._write_batch(batch)
            self._durable_seq = batch[-1]["seq"]

    def _take(self) -> list[dict]:
        batch = []
        while self._buf:
            batch.append(self._buf.popleft())
        return batch

    def _write_batch(self, batch: list[dict]) -> None:
        """Runs on a worker thread. Single writer, so no lock is required."""
        from .encryption import encode_line

        assert self._fh is not None
        self._fh.write("".join(encode_line(r, self._encryptor) + "\n" for r in batch))
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self.flush_cycles += 1

    def _resolve_waiters(self) -> None:
        still: list[tuple[int, asyncio.Future]] = []
        for target, fut in self._waiters:
            if self._durable_seq >= target and not fut.done():
                fut.set_result(None)
            elif not fut.done():
                still.append((target, fut))
        self._waiters = still

    async def close(self) -> None:
        if self._task:
            # Graceful stop, not cancel: signal the loop and await it so any
            # in-flight fsync worker finishes before we touch the handle.
            # Cancelling mid-to_thread would detach that worker and we would
            # close the file out from under a running write.
            self._closing = True
            if self._wake is not None:
                self._wake.set()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._fh:
            self._fh.close()
            self._fh = None

    def records(self) -> list[dict]:
        """Replay support. Reads what is durable on disk, not what is buffered --
        deliberately, so tests assert on crash-survivable state only. Decrypts
        with the configured key; a truncated final line is skipped."""
        from .encryption import decode_line

        if not self.path or not self.path.exists():
            return []
        out = []
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    out.append(decode_line(line, self._encryptor))
                except (json.JSONDecodeError, ValueError):
                    continue  # truncated/corrupt tail
        return out


__all__ = ["AsyncWAL", "BackpressurePolicy", "WALBackpressure", "DROPPED"]
