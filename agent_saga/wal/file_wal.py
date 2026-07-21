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
import logging
import os
from pathlib import Path
from typing import Any, Optional

from .base import (
    _UNSET,
    BackpressurePolicy,
    BufferedWAL,
    DEFAULT_BARRIER_TIMEOUT,
)


logger = logging.getLogger("agent_saga.wal.file")


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

    async def compact(self, *, keep_saga_ids: set) -> int:
        """Rewrite the log keeping only records for the given sagas.

        The default backend grew without bound: a single node running for months
        accumulates every saga it ever ran, and the recovery daemon reads the
        whole file on every sweep. That is the same flaw the Redis backend was
        hardened against, on the backend most people actually run.

        A file can be filtered properly rather than only trimmed from the head,
        so this reclaims every resolved saga, not just a leading run.

        Safety:
          * the io lock is held, so no flush can be writing while the file is
            swapped underneath it;
          * survivors are written to a temp file, fsynced, then os.replace'd --
            atomic on POSIX and Windows, so a crash mid-compaction leaves either
            the old complete log or the new one, never a truncated hybrid;
          * appends during compaction land in the in-memory buffer (append() never
            touches the file) and are flushed to the new file afterwards.
        """
        from ..encryption import encode_line

        if self.path is None:
            return 0

        lock = self._io_lock
        if lock is None:
            return await self._compact_locked(keep_saga_ids, encode_line)
        async with lock:
            return await self._compact_locked(keep_saga_ids, encode_line)

    async def _compact_locked(self, keep_saga_ids: set, encode_line) -> int:
        import os as _os

        if self._fh is not None:
            self._fh.flush()
            _os.fsync(self._fh.fileno())

        records = self.records()
        survivors = [r for r in records if r.get("saga_id") in keep_saga_ids]
        removed = len(records) - len(survivors)
        if removed <= 0:
            return 0

        tmp = self.path.with_suffix(self.path.suffix + ".compact")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("".join(encode_line(r, self._encryptor) + "\n" for r in survivors))
            fh.flush()
            _os.fsync(fh.fileno())

        # Close before replacing: Windows refuses to rename over an open handle.
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        _os.replace(tmp, self.path)
        self._fh = open(self.path, "a", encoding="utf-8")

        logger.info("compacted %d resolved record(s) from %s", removed, self.path)
        return removed

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
