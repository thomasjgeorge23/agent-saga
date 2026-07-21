"""Recovery claim locks.

Two daemons must never compensate the same saga at once. The lock is what makes
that safe. The default is a local filesystem lock (atomic `O_EXCL` create) --
correct on a single host and dependency-free, preserving "install and go".

For a multi-host fleet a filesystem lock over NFS is not trustworthy, so the
lock is an *interface*: inject a Redis/Redlock or database-row lock that
implements `RecoveryLock` and the daemon uses it unchanged. Deliberately, no
distributed backend ships in-tree -- adding Redis as a core dependency would
cost every single-node user the zero-setup path for a feature most do not need.

Idempotency does not depend on the lock. Deterministic recovery tokens plus the
journal already make double-compensation structurally impossible (see
recovery.py); the lock is an efficiency and tidiness guard on top of that, not
the correctness mechanism. So a weaker distributed lock degrades throughput, not
safety.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class RecoveryLock(Protocol):
    def acquire(self, key: str) -> bool:
        """Try to take the lock for `key`. Non-blocking: return True if acquired,
        False if someone else holds it. Never wait."""
        ...

    def release(self, key: str) -> None:
        """Release a lock this instance holds. A no-op if not held."""
        ...


class FileLock:
    """Default lock: one claim file per key, created with O_EXCL.

    Atomic on a local filesystem on both POSIX and Windows. The file records who
    holds it and when, so a stale claim can be diagnosed by hand."""

    def __init__(self, claims_dir: str | Path, *, owner_id: Optional[str] = None):
        self.claims_dir = Path(claims_dir)
        self.owner_id = owner_id or f"{os.getpid()}-{os.urandom(4).hex()}"

    def _path(self, key: str) -> Path:
        safe = key.replace("/", "_").replace("\\", "_")
        return self.claims_dir / f"{safe}.claim"

    def acquire(self, key: str) -> bool:
        self.claims_dir.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self._path(key), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps({"owner_id": self.owner_id, "ts": time.time()}))
        return True

    def release(self, key: str) -> None:
        try:
            self._path(key).unlink()
        except FileNotFoundError:
            pass


class InProcessLock:
    """Single-process lock backed by a set. For an embedded daemon (recovery run
    inside the agent process) or tests, where cross-process files are overkill."""

    def __init__(self) -> None:
        self._held: set[str] = set()

    def acquire(self, key: str) -> bool:
        if key in self._held:
            return False
        self._held.add(key)
        return True

    def release(self, key: str) -> None:
        self._held.discard(key)


__all__ = ["RecoveryLock", "FileLock", "InProcessLock"]
