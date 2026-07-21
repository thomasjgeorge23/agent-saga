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
from typing import Any, Optional

from .base import (
    _UNSET,
    BackpressurePolicy,
    BufferedWAL,
    DEFAULT_BARRIER_TIMEOUT,
    WALStalled,
)

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

    async def read_all(self) -> list[dict]:
        from ..encryption import decode_line

        assert self._client is not None, "RedisWAL not started; call await wal.start()"
        raw = await self._client.lrange(self.key, 0, -1)
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


__all__ = ["RedisWAL"]
