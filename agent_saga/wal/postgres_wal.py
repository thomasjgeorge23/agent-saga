"""PostgreSQL-backed write-ahead log.

WHAT THIS BUYS YOU
    A shared transactional log stored directly in your primary PostgreSQL database,
    eliminating the need to deploy and manage a separate Redis instance for multi-node
    recovery daemon deployments.

Requires the optional dependency: pip install agent-saga[postgres]
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
)

logger = logging.getLogger("agent_saga.wal.postgres")

_IMPORT_HINT = (
    "PostgresWAL needs the 'asyncpg' package, which is an optional dependency.\n"
    "    pip install agent-saga[postgres]\n"
    "The core engine stays dependency-free; only this backend needs it."
)


class PostgresWAL(BufferedWAL):
    """PostgreSQL-backed write-ahead log using asyncpg."""

    def __init__(
        self,
        dsn: Optional[str] = None,
        *,
        host: Optional[str] = None,
        port: Optional[int] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        database: Optional[str] = None,
        table_name: str = "saga_wal",
        pool: Any = None,
        max_buffer: int = 100_000,
        backpressure: BackpressurePolicy = BackpressurePolicy.RAISE,
        encryptor: Any = _UNSET,
        barrier_timeout: Optional[float] = DEFAULT_BARRIER_TIMEOUT,
        chain: bool = True,
    ):
        super().__init__(
            max_buffer=max_buffer,
            backpressure=backpressure,
            encryptor=encryptor,
            barrier_timeout=barrier_timeout,
            chain=chain,
        )
        self.dsn = dsn
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self.table_name = table_name
        self._pool = pool
        self._owns_pool = pool is None

        # Fail early on import error if pool is not injected
        if pool is None:
            try:
                import asyncpg  # noqa: F401
            except ImportError as exc:
                raise ImportError(_IMPORT_HINT) from exc

    async def _open_sink(self) -> None:
        if self._pool is None:
            try:
                import asyncpg
            except ImportError as exc:  # pragma: no cover
                raise ImportError(_IMPORT_HINT) from exc

            # Connect using either dsn or individual parameters
            if self.dsn:
                self._pool = await asyncpg.create_pool(self.dsn)
            else:
                self._pool = await asyncpg.create_pool(
                    host=self.host,
                    port=self.port,
                    user=self.user,
                    password=self.password,
                    database=self.database,
                )

        # Initialize WAL table if not exists
        query = (
            f"CREATE TABLE IF NOT EXISTS {self.table_name} ("
            f"id SERIAL PRIMARY KEY, "
            f"saga_id TEXT, "
            f"step_id TEXT, "
            f"payload JSONB, "
            f"created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            f")"
        )
        async with self._pool.acquire() as conn:
            await conn.execute(query)

    async def _close_sink(self) -> None:
        if self._pool is not None and self._owns_pool:
            await self._pool.close()
        self._pool = None

    async def _flush_batch(self, batch: list[dict]) -> None:
        from ..encryption import encode_line

        assert self._pool is not None

        # Prepare parameters for bulk insert
        data_tuples = []
        for record in batch:
            saga_id = record.get("saga_id")
            step_id = record.get("step_id")
            serialized_str = encode_line(record, self._encryptor)
            payload_json = json.dumps({"data": serialized_str})
            data_tuples.append((saga_id, step_id, payload_json))

        query = (
            f"INSERT INTO {self.table_name} (saga_id, step_id, payload) "
            f"VALUES ($1, $2, $3)"
        )
        async with self._pool.acquire() as conn:
            await conn.executemany(query, data_tuples)
        self.flush_cycles += 1

    async def read_all(self) -> list[dict]:
        assert self._pool is not None, "PostgresWAL not started; call await wal.start()"
        query = f"SELECT payload FROM {self.table_name} ORDER BY id ASC"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query)

        from ..encryption import decode_line
        records = []
        for row in rows:
            payload_val = row["payload"]
            # Check if payload_val is a string or dict (asyncpg may auto-decode jsonb to dict)
            if isinstance(payload_val, str):
                payload_dict = json.loads(payload_val)
            else:
                payload_dict = payload_val

            serialized_str = payload_dict.get("data", "")
            if serialized_str:
                try:
                    records.append(decode_line(serialized_str, self._encryptor))
                except (json.JSONDecodeError, ValueError):
                    continue
        return records

    async def clear(self) -> None:
        assert self._pool is not None, "PostgresWAL not started; call await wal.start()"
        self._buf.clear()
        self._seq = 0
        self._durable_seq = 0
        query = f"DELETE FROM {self.table_name}"
        async with self._pool.acquire() as conn:
            await conn.execute(query)
