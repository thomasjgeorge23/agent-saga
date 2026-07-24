"""A WAL whose durability is delegated to an async storage sink.

`FileWAL` persists with `write() + os.fsync()`. That is exactly what an edge
runtime -- a Cloudflare Worker, Deno Deploy, a browser -- cannot offer: there is
no synchronous durable disk there. Storage is an *async* API (Workers KV/D1/R2,
Durable Objects, OPFS), durable-on-ack rather than durable-on-fsync.

`AsyncSinkWAL` bridges that gap by moving *only* the storage step. It inherits
the whole engine -- the batching, the hash chain, the barrier machinery, the
gate -- from `BufferedWAL`, and overrides the one method that touches the disk:

    wal = AsyncSinkWAL(sink=my_kv_put)     # my_kv_put(records) -> awaitable
    await wal.start()
    ...                                     # gate, chain, barrier all as usual

`sink(records)` is any async callable that persists a batch and returns once the
storage service has acknowledged it. That ack is the durability boundary -- and
it is honestly weaker than fsync: a crash between buffer and ack loses the
un-acked tail. This is the deliberate trade of edge durability, and it is
documented rather than hidden. See docs/EDGE_WASM_FEASIBILITY.md.

The point of shipping this in the server package is the proof it embodies: the
existing WAL test suite passes against this class with an in-memory sink, which
demonstrates that the safety engine is genuinely separable from the disk. That
is the go/no-go gate for a real edge port.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional

from .base import BufferedWAL, BackpressurePolicy, DEFAULT_BARRIER_TIMEOUT, _UNSET

logger = logging.getLogger("agent_saga.wal.async_sink")


class AsyncStorageSink:
    """The interface an edge sink must satisfy.

    A WAL is not write-only: replay, audit, and recovery all read it back, so the
    sink has to be a *store*, not a fire-and-forget pipe. This is the smallest
    contract that a Workers KV / D1 / R2 / OPFS adapter can implement:

      * ``append(lines)`` -- persist encoded lines, resolve on storage ack
      * ``scan()``        -- yield every persisted line, in order
      * ``truncate()``    -- discard all lines (tests / operator tooling)

    ``lines`` are already-encoded strings (the same on-the-wire form the file
    backend writes), so an adapter only has to move bytes.
    """

    async def append(self, lines: list[str]) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def scan(self) -> list[str]:  # pragma: no cover - interface
        raise NotImplementedError

    async def truncate(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class InMemoryAsyncSink(AsyncStorageSink):
    """A reference sink that keeps lines in a list. Stands in for a real storage
    service in tests and proves the WAL engine is separable from the disk."""

    def __init__(self) -> None:
        self._lines: list[str] = []

    async def append(self, lines: list[str]) -> None:
        self._lines.extend(lines)

    async def scan(self) -> list[str]:
        return list(self._lines)

    async def truncate(self) -> None:
        self._lines.clear()


class AsyncSinkWAL(BufferedWAL):
    """A WAL that persists through an async storage sink instead of fsync.

    Everything above the disk -- the gate, the hash chain, batching, the barrier
    -- is inherited unchanged from BufferedWAL. Only the three storage methods
    are overridden to talk to the sink.

    Durability is whatever the sink guarantees on the awaited ``append``. For a
    durable-on-ack store (KV/D1/R2) that is real, if asynchronous, durability --
    honestly weaker than fsync, since a crash between buffer and ack loses the
    un-acked tail. Match the sink to the workload; see
    docs/EDGE_WASM_FEASIBILITY.md.
    """

    def __init__(
        self,
        sink: AsyncStorageSink,
        *,
        max_buffer: int = 100_000,
        backpressure: BackpressurePolicy = BackpressurePolicy.RAISE,
        encryptor: Any = _UNSET,
        barrier_timeout: Optional[float] = DEFAULT_BARRIER_TIMEOUT,
        chain: bool = True,
    ):
        super().__init__(max_buffer=max_buffer, backpressure=backpressure,
                         encryptor=encryptor, barrier_timeout=barrier_timeout,
                         chain=chain)
        for m in ("append", "scan", "truncate"):
            if not callable(getattr(sink, m, None)):
                raise TypeError(f"sink must implement async {m}() (see AsyncStorageSink)")
        self._sink = sink
        self.persisted = 0            # records the sink has acknowledged

    async def _flush_batch(self, batch: list[dict]) -> None:
        """Encode as the file backend does, then hand the lines to the sink and
        wait for its ack. If the sink raises, the batch is not acknowledged and
        the barrier fails -- the caller learns the write did not land, the same
        contract fsync gives, just over the network."""
        from ..encryption import encode_line

        lines = [encode_line(r, self._encryptor) for r in batch]
        await self._sink.append(lines)
        self.persisted += len(batch)

    async def read_all(self) -> list[dict]:
        from ..encryption import decode_line

        out: list[dict] = []
        for line in await self._sink.scan():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(decode_line(line, self._encryptor))
            except Exception:
                continue          # tolerate a truncated tail, as the file backend does
        return out

    async def clear(self) -> None:
        await self._sink.truncate()

    async def _close_sink(self) -> None:
        # The sink owns its own connection lifecycle; the tail was already drained
        # through the normal flush before close.
        return


__all__ = ["AsyncSinkWAL", "AsyncStorageSink", "InMemoryAsyncSink"]
