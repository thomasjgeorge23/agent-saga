"""Redis-backed write-ahead log, for multi-node deployments.

WHAT THIS BUYS YOU
    One shared log across every pod. A recovery daemon on node B can see sagas
    started on node A, which a local file cannot offer no matter how it is
    mounted.

WHAT IT COSTS YOU -- READ THIS BEFORE USING IT FOR MONEY
    Redis is not, by default, a durable log. With the usual `appendfsync
    everysec` it acknowledges a write and can lose it up to a second later if
    the node dies; with `appendfsync no` the window is larger still. Worse, in a
    replicated setup a failover can lose writes the primary already
    acknowledged, because replication is asynchronous.

    So `barrier()` here cannot mean what it means on a local disk. It means:
    "Redis has acknowledged this write, and -- if `wait_replicas` is set -- at
    least that many replicas have acknowledged it too." That is a weaker
    guarantee than fsync-to-disk, and this class will not pretend otherwise.

    For a financial ledger, run Redis with `appendfsync always` AND set
    `wait_replicas>=1`, or keep the FileWAL on a durable volume. The engine's
    safety argument is only as strong as the fence underneath it.

Requires the optional dependency:  pip install agent-saga[redis]
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from .base import (
    _UNSET,
    BackpressurePolicy,
    BufferedWAL,
    DEFAULT_BARRIER_TIMEOUT,
    WALStalled,
)

logger = logging.getLogger("agent_saga.wal.redis")

_IMPORT_HINT = (
    "RedisWAL needs the 'redis' package, which is an optional dependency.\n"
    "    pip install agent-saga[redis]\n"
    "The core engine stays dependency-free; only this backend needs it."
)


class RedisWAL(BufferedWAL):
    """Append-only log stored in a Redis list.

    A list (RPUSH/LRANGE) rather than a stream: the engine assigns its own
    monotonic sequence numbers already, so server-side ids would be a second
    source of truth to reconcile. Records are encoded with the same line codec
    as the file backend, so BYOK encryption applies here unchanged -- the values
    in Redis are ciphertext when a key is configured.
    """

    def __init__(
        self,
        url: str = "redis://localhost:6379/0",
        *,
        key: str = "agent-saga:wal",
        seq_key: Optional[str] = None,
        client: Any = None,
        wait_replicas: int = 0,
        wait_timeout_ms: int = 1000,
        max_buffer: int = 100_000,
        backpressure: BackpressurePolicy = BackpressurePolicy.RAISE,
        encryptor: Any = _UNSET,
        barrier_timeout: Optional[float] = DEFAULT_BARRIER_TIMEOUT,
    ):
        super().__init__(max_buffer=max_buffer, backpressure=backpressure,
                         encryptor=encryptor, barrier_timeout=barrier_timeout)
        self.url = url
        self.key = key
        # Global sequence counter, shared by every node writing this log. See
        # _flush_batch for why the per-process counter is not enough.
        self.seq_key = seq_key or f"{key}:seq"
        self.wait_replicas = wait_replicas
        self.wait_timeout_ms = wait_timeout_ms
        self._client = client
        self._owns_client = client is None

        if client is None:
            # Fail at construction, not on the first append halfway through a
            # saga. Import eagerly here precisely so the error arrives while the
            # developer is still wiring things up.
            try:
                import redis.asyncio  # noqa: F401
            except ImportError as exc:
                raise ImportError(_IMPORT_HINT) from exc

    # -- sink --------------------------------------------------------------

    async def _open_sink(self) -> None:
        if self._client is None:
            try:
                from redis.asyncio import Redis
            except ImportError as exc:  # pragma: no cover - guarded in __init__
                raise ImportError(_IMPORT_HINT) from exc
            self._client = Redis.from_url(self.url, decode_responses=True)

    async def _close_sink(self) -> None:
        # Only close what we opened. An injected client belongs to the caller,
        # who may well be sharing it with the rest of their application.
        if self._client is not None and self._owns_client:
            close = getattr(self._client, "aclose", None) or getattr(self._client, "close", None)
            if close is not None:
                await close()
        self._client = None

    async def _flush_batch(self, batch: list[dict]) -> None:
        from ..encryption import encode_line

        assert self._client is not None

        # Stamp a GLOBAL sequence number.
        #
        # `seq` is a per-process counter, so every node writing this shared key
        # starts again at 1 and the log ends up full of duplicates. That does not
        # break per-saga rollback order (within one saga the local seqs are still
        # monotonic, so a stable sort keeps that saga's steps in order), but it
        # does destroy three things that matter: seq stops being a unique id,
        # cross-saga ordering becomes meaningless, and cursor reads ("everything
        # after N") become impossible.
        #
        # INCRBY reserves a contiguous range for this batch in one round trip, so
        # the cost is per-flush rather than per-record, and group commit amortises
        # it exactly like the write itself. `seq` is deliberately left untouched:
        # the barrier bookkeeping compares against the local counter, and
        # overwriting it here would make every fence resolve instantly.
        try:
            end = int(await self._client.incrby(self.seq_key, len(batch)))
            start = end - len(batch) + 1
            for offset, record in enumerate(batch):
                record["gseq"] = start + offset
        except AttributeError:
            # A client without INCRBY (an old stub). Degrade to local ordering
            # rather than refusing to write -- but say so, because the log's
            # global ordering guarantee is then not being met.
            logger.warning(
                "Redis client has no INCRBY; records will carry only a "
                "process-local seq, so cross-node ordering is not guaranteed.")

        lines = [encode_line(r, self._encryptor) for r in batch]
        await self._client.rpush(self.key, *lines)

        if self.wait_replicas:
            # The strongest fence Redis offers: block until N replicas have the
            # write. Still not fsync -- see the module docstring.
            acked = await self._client.wait(self.wait_replicas, self.wait_timeout_ms)
            if acked is not None and int(acked) < self.wait_replicas:
                raise WALStalled(
                    f"only {acked} of {self.wait_replicas} Redis replica(s) "
                    f"acknowledged within {self.wait_timeout_ms}ms; refusing to "
                    f"report this batch as durable."
                )
        self.flush_cycles += 1

    # -- reading -----------------------------------------------------------

    async def read_all(self, *, chunk: int = 5_000) -> list[dict]:
        """Every record, read in bounded chunks.

        `LRANGE key 0 -1` materialises the entire history in one reply on both
        the server and the client. Paging keeps *peak* memory proportional to
        `chunk` rather than to the age of the log. It still returns everything --
        `compact()` is what stops the log growing without bound.
        """
        assert self._client is not None, "RedisWAL not started; call await wal.start()"
        raw: list = []
        start = 0
        while True:
            page = await self._client.lrange(self.key, start, start + chunk - 1)
            if not page:
                break
            raw.extend(page)
            if len(page) < chunk:
                break
            start += chunk
        return self._decode(raw)

    async def read_since(self, gseq: int, *, chunk: int = 5_000) -> list[dict]:
        """Records with a global sequence greater than `gseq`.

        The cursor a long-running daemon wants: sweep N+1 only pays for what
        arrived since sweep N.
        """
        return [r for r in await self.read_all(chunk=chunk)
                if (r.get("gseq") or r.get("seq", 0)) > gseq]

    async def compact(self, *, keep_saga_ids: set, chunk: int = 1_000) -> int:
        """Drop resolved history from the head of the log.

        A Redis list cannot cheaply delete from the middle, so this trims only
        the leading run of records that belong to no saga in `keep_saga_ids`.
        That is deliberately conservative: it can never remove a record an
        unresolved saga still needs, and in practice it reclaims almost
        everything, because sagas resolve roughly in the order they started.

        `LTRIM start -1` is atomic and safe against a concurrent RPUSH -- a new
        record lands past the end and survives. Returns the number removed.
        """
        assert self._client is not None, "RedisWAL not started; call await wal.start()"
        watermark = 0
        start = 0
        done = False
        while not done:
            page = await self._client.lrange(self.key, start, start + chunk - 1)
            if not page:
                break
            for offset, record in enumerate(self._decode(list(page))):
                if record.get("saga_id") in keep_saga_ids:
                    watermark = start + offset
                    done = True
                    break
            else:
                start += len(page)
                watermark = start
                if len(page) < chunk:
                    done = True
                continue
        if watermark <= 0:
            return 0
        await self._client.ltrim(self.key, watermark, -1)
        logger.info("compacted %d resolved record(s) from %s", watermark, self.key)
        return watermark

    def _decode(self, raw: list) -> list[dict]:
        from ..encryption import decode_line

        out: list[dict] = []
        for line in raw:
            if isinstance(line, bytes):
                line = line.decode("utf-8")
            if not line.strip():
                continue
            try:
                out.append(decode_line(line, self._encryptor))
            except (json.JSONDecodeError, ValueError):
                # Same stance as the file backend: a record we cannot parse is
                # skipped, never fatal. An encrypted record with no key still
                # raises, because reading that as "absent" would silently
                # abandon a crashed saga.
                continue
        return out

    async def clear(self) -> None:
        assert self._client is not None, "RedisWAL not started; call await wal.start()"
        self._buf.clear()
        self._seq = 0
        self._durable_seq = 0
        await self._client.delete(self.key)
        try:
            await self._client.delete(self.seq_key)
        except Exception:
            pass


__all__ = ["RedisWAL"]
