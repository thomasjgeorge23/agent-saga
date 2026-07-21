"""Write-ahead log: the contract, and the buffering machinery every backend shares.

`BaseWAL` is deliberately not a three-method `append/read_all/clear` interface.
The engine's entire safety property is *intent is durable before the side
effect*, and that is enforced by exactly one call: `barrier()`. A backend
without a fence is fire-and-forget, and a crash against it orphans real charges
with no record to recover from. So `barrier()` is a first-class part of the
contract, and a backend that cannot implement it honestly must say so rather
than no-op it.

`BufferedWAL` carries the parts that are hard to get right and identical for
every sink: the synchronous hot-path append, sequence numbering, the
backpressure policy, group commit, and the barrier/waiter bookkeeping. A backend
implements only where the bytes go. Duplicating this per backend is how two
implementations silently diverge on durability.
"""

from __future__ import annotations

import abc
import asyncio
import enum
import time
from collections import deque
from typing import Any, Optional

DROPPED = -1
"""append() sentinel: the record was NOT written. Never a valid seq."""

_UNSET = object()
"""Distinguishes 'no encryptor argument given' (resolve from env) from an
explicit None (force plaintext)."""

DEFAULT_BARRIER_TIMEOUT = 30.0
"""Seconds a durability fence may wait before giving up.

An unbounded wait is a silent failure in the same family as a dropped record: if
the volume fills or the device wedges, `barrier()` never returns and the agent
hangs forever with no error, no log, and no rollback. Thirty seconds is far
beyond any healthy fsync (which is single-digit milliseconds, amortized further
by group commit) and short enough that a wedged disk surfaces as a real error."""


class BackpressurePolicy(enum.Enum):
    """What append() does when the in-memory buffer is full.

    A silently dropped record is a silently unrecoverable side effect: if the
    STEP_INTENT for a charge never lands, the recovery daemon has no way to undo
    it. So the default is RAISE -- fail the step *before* its effect runs, rather
    than let the agent proceed with an incomplete rollback history.
    """

    RAISE = "RAISE"
    BLOCK = "BLOCK"
    DROP_SILENT = "DROP_SILENT"


class WALBackpressure(RuntimeError):
    """The WAL buffer is full and the policy is RAISE. Raised before any side
    effect, so the transaction is safely abortable."""


class WALStalled(RuntimeError):
    """A durability fence exceeded its timeout.

    The sink is not acknowledging writes -- a full volume, a wedged device, an
    unreachable Redis. Raised so the saga fails loudly and refuses to perform a
    side effect it could not durably record, instead of hanging forever.
    """


class BaseWAL(abc.ABC):
    """The contract the engine depends on."""

    @abc.abstractmethod
    async def start(self) -> None:
        """Open the sink and begin flushing. Must be called before use."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Drain, then release the sink."""

    @abc.abstractmethod
    def append(self, event: str, payload: dict) -> int:
        """Record an event. SYNCHRONOUS by contract: this sits on the hot path
        between an agent and every tool call, and making it a coroutine would
        add an event-loop scheduling hop to each one. Returns the sequence
        number, or DROPPED."""

    @abc.abstractmethod
    async def barrier(self, seq: Optional[int] = None) -> None:
        """Return only once every record up to `seq` is durable.

        This is the fence the whole engine is built on. A backend that cannot
        provide a real durability guarantee must document exactly what its
        barrier does guarantee -- never silently return early.

        Raises WALStalled if the sink does not acknowledge in time.
        """

    @abc.abstractmethod
    async def read_all(self) -> list[dict]:
        """Every durable record, in sequence order. Used for replay and audit."""

    @abc.abstractmethod
    async def clear(self) -> None:
        """Discard all records. Intended for tests and operator tooling."""

    async def ensure_capacity(self) -> None:
        """Optional hook: yield until the buffer has room. No-op by default."""
        return

    async def compact(self, *, keep_saga_ids: set) -> int:
        """Drop records belonging to no saga in `keep_saga_ids`. Returns the
        count removed.

        Not abstract, so a custom backend is not forced to implement it -- but
        it raises rather than returning 0, because a silent no-op would let an
        operator believe their log was being bounded when it was growing without
        limit.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement compaction; its log will "
            f"grow without bound unless you trim it yourself."
        )


class BufferedWAL(BaseWAL):
    """Shared machinery: buffer, sequencing, backpressure, group commit, fences.

    Subclasses implement only the sink:
        _open_sink()  /  _close_sink()
        _flush_batch(batch)          -- async, must be durable when it returns
        read_all()  /  clear()
    """

    def __init__(
        self,
        *,
        max_buffer: int = 100_000,
        backpressure: BackpressurePolicy = BackpressurePolicy.RAISE,
        encryptor: Any = _UNSET,
        barrier_timeout: Optional[float] = DEFAULT_BARRIER_TIMEOUT,
        chain: bool = True,
    ):
        self.chain = chain
        """Hash-chain every record, making the log tamper-evident.

        On by default: the cost is one SHA-256 over a small dict per record --
        microseconds, off the caller's thread -- and a log that is only
        *sometimes* chained is not evidence of anything. Turn it off only for a
        throwaway or test log."""
        self._chain_head: str = ""
        """Set from the last record already on disk when the sink opens, so a
        restart continues one chain instead of starting a second one that
        silently proves nothing about the first."""
        self._buf: deque = deque()
        self._seq = 0
        self._durable_seq = 0
        self._waiters: list[tuple[int, asyncio.Future]] = []
        self._wake: Optional[asyncio.Event] = None
        self._task: Optional[asyncio.Task] = None
        self._closing = False
        self._max_buffer = max_buffer
        self.backpressure = backpressure
        self.barrier_timeout = barrier_timeout
        self._encryptor_arg = encryptor
        self._encryptor = None
        self._io_lock: Optional[asyncio.Lock] = None
        """Serialises flushing against compaction. A rewrite swaps the file out
        from under the writer, so the two must never overlap."""
        self.dropped = 0
        """Count of records shed under DROP_SILENT. Non-zero means rollback
        history is incomplete -- surface it, never hide it."""
        self.barriers = 0
        """How many callers actually blocked waiting for durability."""
        self.flush_cycles = 0
        """How many flushes we actually performed. barriers/flush_cycles is the
        group-commit amortization factor."""

    # -- sink hooks --------------------------------------------------------

    async def _open_sink(self) -> None:
        return

    async def _close_sink(self) -> None:
        return

    @abc.abstractmethod
    async def _flush_batch(self, batch: list[dict]) -> None:
        """Persist a batch. Must be durable by the time it returns."""

    def _flush_sync_best_effort(self) -> None:
        """Last-ditch synchronous drain during cancellation. Backends that
        cannot write synchronously leave this as a no-op."""
        self._take()

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        from ..encryption import get_wal_encryptor

        self._encryptor = (get_wal_encryptor() if self._encryptor_arg is _UNSET
                           else self._encryptor_arg)
        await self._open_sink()
        self._wake = asyncio.Event()
        self._io_lock = asyncio.Lock()
        self._task = asyncio.create_task(self._flush_loop())

    async def close(self) -> None:
        if self._task:
            # Graceful stop, not cancel: signal the loop and await it so any
            # in-flight write finishes before we release the sink.
            #
            # Bounded, though. If the sink is wedged the flush loop never
            # returns, and an unbounded await here would hang shutdown forever
            # while still holding the file handle -- the same silent-hang class
            # of bug the barrier timeout exists to remove. After the grace
            # period we cancel and release the sink regardless.
            self._closing = True
            if self._wake is not None:
                self._wake.set()
            grace = self.barrier_timeout if self.barrier_timeout is not None else 30.0
            done, pending = await asyncio.wait({self._task}, timeout=grace)
            if pending:
                self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                # A sink failure or cancellation during teardown must not stop us
                # from releasing the handle; pending fences were already failed.
                pass
            self._task = None
        await self._close_sink()

    # -- hot path ----------------------------------------------------------

    def append(self, event: str, payload: dict) -> int:
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
        if self.backpressure is not BackpressurePolicy.BLOCK:
            return
        while len(self._buf) >= self._max_buffer and self._task is not None:
            if self._wake is not None:
                self._wake.set()
            await asyncio.sleep(0.001)

    async def barrier(self, seq: Optional[int] = None) -> None:
        target = self._seq if seq is None else seq
        if self._durable_seq >= target:
            return
        if self._task is None:
            raise RuntimeError("WAL not started; call await wal.start()")
        self.barriers += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        entry = (target, fut)
        self._waiters.append(entry)
        if self._wake is not None:
            self._wake.set()

        if self.barrier_timeout is None:
            await fut
            return
        try:
            await asyncio.wait_for(asyncio.shield(fut), self.barrier_timeout)
        except asyncio.TimeoutError:
            # Stop tracking this waiter so a late flush does not resolve a future
            # nobody is awaiting any more.
            try:
                self._waiters.remove(entry)
            except ValueError:
                pass
            raise WALStalled(
                f"WAL did not reach durability for seq {target} within "
                f"{self.barrier_timeout}s. The sink is not acknowledging writes "
                f"(full volume, wedged device, unreachable backend). Refusing to "
                f"report this intent as durable."
            ) from None

    # -- flushing ----------------------------------------------------------

    async def _flush_loop(self) -> None:
        assert self._wake is not None
        try:
            while True:
                await self._wake.wait()
                self._wake.clear()
                await self._flush_once()
                if self._closing:
                    await self._flush_once()
                    return
        except asyncio.CancelledError:
            self._flush_sync_best_effort()
            self._resolve_waiters()
            raise

    async def _flush_once(self) -> None:
        # Take everything queued as one batch. Concurrent barriers landing in
        # the same cycle share a single flush -- group commit, for free, as a
        # consequence of batching.
        batch = self._take()
        if batch:
            if self.chain:
                # On the flusher thread, in sequence order, single writer -- the
                # only place the chain can be built without a lock and without
                # any chance of two records interleaving.
                from ..integrity import GENESIS, stamp_batch

                if not self._chain_head:
                    self._chain_head = GENESIS
                self._chain_head = stamp_batch(batch, self._chain_head)
            try:
                if self._io_lock is not None:
                    async with self._io_lock:
                        await self._flush_batch(batch)
                else:
                    await self._flush_batch(batch)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                # The sink rejected the write. Fail every pending fence now, with
                # the real cause, instead of letting them wait out the timeout:
                # a caller must learn *why* durability failed, and learn it as
                # early as possible, because it is still before the side effect.
                self._fail_waiters(exc)
                return
            self._durable_seq = batch[-1]["seq"]
        self._resolve_waiters()

    def _fail_waiters(self, exc: BaseException) -> None:
        failure = WALStalled(f"WAL sink failed to persist a batch: {exc!r}")
        failure.__cause__ = exc
        for _target, fut in self._waiters:
            if not fut.done():
                fut.set_exception(failure)
        self._waiters = []

    def _take(self) -> list[dict]:
        batch = []
        while self._buf:
            batch.append(self._buf.popleft())
        return batch

    def _resolve_waiters(self) -> None:
        still: list[tuple[int, asyncio.Future]] = []
        for target, fut in self._waiters:
            if self._durable_seq >= target and not fut.done():
                fut.set_result(None)
            elif not fut.done():
                still.append((target, fut))
        self._waiters = still


__all__ = [
    "BaseWAL",
    "BufferedWAL",
    "BackpressurePolicy",
    "WALBackpressure",
    "WALStalled",
    "DROPPED",
    "DEFAULT_BARRIER_TIMEOUT",
]
