"""Write-ahead log backends.

`AsyncWAL` (== `FileWAL`) remains the default and the zero-dependency one, so
every existing `from agent_saga.wal import AsyncWAL` keeps working. `RedisWAL`
is imported lazily: touching this package must not require `redis` to be
installed.
"""

from __future__ import annotations

from typing import Any

from .base import (
    DEFAULT_BARRIER_TIMEOUT,
    DROPPED,
    BackpressurePolicy,
    BaseWAL,
    BufferedWAL,
    WALBackpressure,
    WALStalled,
)
from .file_wal import AsyncWAL, FileWAL
from .mmap_wal import MmapWAL


def __getattr__(name: str) -> Any:
    """Expose RedisWAL and PostgresWAL without importing driver packages at package import time."""
    if name == "RedisWAL":
        from .redis_wal import RedisWAL
        return RedisWAL
    if name == "PostgresWAL":
        from .postgres_wal import PostgresWAL
        return PostgresWAL
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "BaseWAL",
    "BufferedWAL",
    "AsyncWAL",
    "FileWAL",
    "MmapWAL",
    "RedisWAL",
    "PostgresWAL",
    "BackpressurePolicy",
    "WALBackpressure",
    "WALStalled",
    "DROPPED",
    "DEFAULT_BARRIER_TIMEOUT",
]
