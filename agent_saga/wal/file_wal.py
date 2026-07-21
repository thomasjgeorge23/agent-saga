"""File-backed write-ahead log -- the zero-dependency default.

The durability story is deliberately tiered:

  * append()  -- synchronous, in-process, lock-free deque push. Sub-microsecond.
                 Survives a caught exception. Does NOT survive SIGKILL.
  * barrier() -- awaits flush + fsync. A real disk round trip. Survives SIGKILL.

We pay for the barrier only where losing the record is unacceptable: before any
COMPENSABLE or IRREVERSIBLE effect. REVERSIBLE steps ride the fast path. That
tiering is why the hot-path overhead is defensible; a WAL that fsynced every
event would not be.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Optional

from .base import (
    _UNSET,
    BackpressurePolicy,
    BufferedWAL,
    DEFAULT_BARRIER_TIMEOUT,
)


class FileWAL(BufferedWAL):
    """Append-only JSON-lines log on local disk.

    `path=None` keeps everything in memory -- useful for tests and for sagas
    whose steps are all REVERSIBLE, where nothing needs to survive a crash.
    """

    def __init__(
        self,
        path: Optional[str | Path] = None,
        *,
        max_buffer: int = 100_000,
        backpressure: BackpressurePolicy = BackpressurePolicy.RAISE,
        encryptor: Any = _UNSET,
        barrier_timeout: Optional[float] = DEFAULT_BARRIER_TIMEOUT,
    ):
        super().__init__(max_buffer=max_buffer, backpressure=backpressure,
                         encryptor=encryptor, barrier_timeout=barrier_timeout)
        self.path = Path(path) if path else None
        self._fh = None
        self._flush_pool = None

    # -- sink --------------------------------------------------------------

    async def _open_sink(self) -> None:
        from ..executors import new_wal_executor

        # Private to this WAL. A starved flusher blocks every barrier() in the
        # process, so it must never queue behind arbitrary tool calls.
        self._flush_pool = new_wal_executor()
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.path, "a", encoding="utf-8")

    async def _close_sink(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None
        if self._flush_pool is not None:
            # The flush task has already finished, so nothing is queued; this
            # just joins the idle worker so the thread does not outlive the WAL.
            self._flush_pool.shutdown(wait=True)
            self._flush_pool = None

    async def _flush_batch(self, batch: list[dict]) -> None:
        if self._fh is None:
            return
        # fsync is a blocking syscall, so it runs off the loop -- on this WAL's
        # private single-thread pool, never the shared tool pool.
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._flush_pool, self._write_batch, batch)

    def _flush_sync_best_effort(self) -> None:
        batch = self._take()
        if batch:
            if self._fh is not None:
                self._write_batch(batch)
            self._durable_seq = batch[-1]["seq"]

    def _write_batch(self, batch: list[dict]) -> None:
        """Runs on the private worker thread. Single writer, so no lock."""
        from ..encryption import encode_line

        assert self._fh is not None
        self._fh.write("".join(encode_line(r, self._encryptor) + "\n" for r in batch))
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self.flush_cycles += 1

    # -- reading -----------------------------------------------------------

    def records(self) -> list[dict]:
        """Synchronous replay of what is durable on disk -- deliberately not
        what is buffered, so callers only ever see crash-survivable state.
        Decrypts with the configured key; a truncated final line is skipped."""
        from ..encryption import decode_line

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

    async def read_all(self) -> list[dict]:
        return self.records()

    async def clear(self) -> None:
        self._buf.clear()
        self._seq = 0
        self._durable_seq = 0
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        if self.path and self.path.exists():
            self.path.unlink()
        if self.path:
            self._fh = open(self.path, "a", encoding="utf-8")


# The original public name. Everything in the wild constructs `AsyncWAL(path)`,
# and a file-backed WAL remains the default, so this stays the primary spelling.
AsyncWAL = FileWAL

__all__ = ["FileWAL", "AsyncWAL"]
