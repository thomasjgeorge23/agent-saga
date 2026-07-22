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
import threading
import time
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable


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

    def __init__(self, claims_dir: str | Path, *, owner_id: Optional[str] = None, ttl_seconds: Optional[float] = 300.0):
        self.claims_dir = Path(claims_dir)
        self.owner_id = owner_id or f"{os.getpid()}-{os.urandom(4).hex()}"
        self.ttl_seconds = ttl_seconds

    def _path(self, key: str) -> Path:
        safe = key.replace("/", "_").replace("\\", "_")
        return self.claims_dir / f"{safe}.claim"

    def acquire(self, key: str) -> bool:
        self.claims_dir.mkdir(parents=True, exist_ok=True)
        path = self._path(key)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if self.ttl_seconds is not None and path.exists():
                try:
                    stat = path.stat()
                    if time.time() - stat.st_mtime > self.ttl_seconds:
                        try:
                            path.unlink()
                        except FileNotFoundError:
                            pass
                        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    else:
                        return False
                except (OSError, FileExistsError):
                    return False
            else:
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


# ---------------------------------------------------------------------------
# Semantic locks
# ---------------------------------------------------------------------------

class SemanticLockConflictError(RuntimeError):
    """Another saga holds this resource.

    Raised *before* the step runs, so the conflicting work never happens -- the
    same stance as the pre-flight gate. Losing a race is not an error to
    swallow; it means someone else is mid-transaction on that account.
    """

    def __init__(self, resource_id: str, owner: str, waiter: str):
        self.resource_id, self.owner, self.waiter = resource_id, owner, waiter
        super().__init__(
            f"resource {resource_id!r} is semantically locked by saga {owner} "
            f"(requested by saga {waiter}). Another saga is mid-transaction on "
            f"it; proceeding would risk a dirty read or a lost update."
        )


class LockAcquisitionTimeoutError(SemanticLockConflictError):
    """Raised when a lock cannot be acquired within the specified timeout."""

    def __init__(self, resource_id: str, owner: str, waiter: str, timeout: float):
        super().__init__(resource_id, owner, waiter)
        self.timeout = timeout
        self.args = (
            f"Failed to acquire semantic lock for resource {resource_id!r} within {timeout} seconds. "
            f"Lock is currently semantically locked by saga {owner} (requested by saga {waiter}).",
        )


class SemanticLockManager:
    """Application-level locks over business resources, held for a saga's life.

    Sagas have no ACID isolation: each step commits immediately, so a second
    saga can read a balance the first has already tentatively spent. A semantic
    lock is the standard countermeasure -- it does not lock database rows (which
    would reintroduce the long-held-transaction problem the saga pattern exists
    to avoid); it marks a *business* resource as claimed, and other sagas
    respect that claim.

    Re-entrant per saga: the same saga_id may take the same resource repeatedly,
    because a multi-step workflow naturally touches one account more than once.

    SCOPE: this default is process-local, exactly like FileLock. It is correct
    for one process and it is NOT a distributed lock. Inject a shared
    implementation when sagas for the same resource can run on different nodes.
    """

    distributed = False

    def __init__(self) -> None:
        self._owners: dict[str, str] = {}     # resource_id -> saga_id
        self._expirations: dict[str, float] = {}  # resource_id -> expiration timestamp
        self._heartbeats: dict[str, asyncio.Task] = {}  # resource_id -> heartbeat task
        self._mutex = threading.Lock()

    def try_acquire(self, resource_id: str, saga_id: str, ttl: Optional[float] = None) -> bool:
        with self._mutex:
            now = time.monotonic()
            if resource_id in self._expirations and now > self._expirations[resource_id]:
                self._clear_lock_internal(resource_id)
            owner = self._owners.get(resource_id)
            if owner is None:
                self._owners[resource_id] = saga_id
                if ttl is not None:
                    self._expirations[resource_id] = now + ttl
                return True
            if owner == saga_id:
                if ttl is not None:
                    self._expirations[resource_id] = now + ttl
                return True
            return False

    async def acquire(self, resource_id: str, saga_id: str, *,
                      timeout: float = 5.0, poll: float = 0.01,
                      ttl: Optional[float] = None) -> None:
        """Claim a resource for a saga.

        `timeout=0` fails fast, which is usually right for an agent: blocking a
        model mid-run is worse than telling it the resource is busy. A positive
        timeout waits, yielding to the loop so other sagas keep progressing.
        """
        if self.try_acquire(resource_id, saga_id, ttl=ttl):
            if ttl is not None:
                self._start_heartbeat(resource_id, saga_id, ttl)
            return
        if timeout > 0:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                await asyncio.sleep(poll)
                if self.try_acquire(resource_id, saga_id, ttl=ttl):
                    if ttl is not None:
                        self._start_heartbeat(resource_id, saga_id, ttl)
                    return
        raise LockAcquisitionTimeoutError(resource_id, self.owner(resource_id) or "?", saga_id, timeout)

    def _start_heartbeat(self, resource_id: str, saga_id: str, ttl: float) -> None:
        with self._mutex:
            old_task = self._heartbeats.pop(resource_id, None)
            if old_task:
                old_task.cancel()
                try:
                    loop = asyncio.get_running_loop()
                    if loop.is_running():
                        loop.create_task(self._await_cancelled_task(old_task))
                except RuntimeError:
                    pass
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(self._heartbeat_loop(resource_id, saga_id, ttl))
                self._heartbeats[resource_id] = task
            except RuntimeError:
                pass

    async def _heartbeat_loop(self, resource_id: str, saga_id: str, ttl: float) -> None:
        interval = ttl / 2.0
        try:
            while True:
                await asyncio.sleep(interval)
                renewed = self.renew(resource_id, saga_id, ttl)
                if not renewed:
                    break
        except asyncio.CancelledError:
            raise

    def renew(self, resource_id: str, saga_id: str, ttl: float) -> bool:
        with self._mutex:
            if self._owners.get(resource_id) == saga_id:
                self._expirations[resource_id] = time.monotonic() + ttl
                return True
            return False

    async def _await_cancelled_task(self, task: asyncio.Task) -> None:
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    def release(self, resource_id: str, saga_id: str) -> bool:
        """Release one resource, but only if this saga actually holds it --
        never let one saga free another's claim."""
        with self._mutex:
            if self._owners.get(resource_id) == saga_id:
                self._clear_lock_internal(resource_id)
                return True
            return False

    def release_all(self, saga_id: str) -> list[str]:
        """Drop every claim held by a saga. Called from the saga boundary, so a
        crashed or aborted saga cannot strand a resource forever."""
        with self._mutex:
            held = [r for r, owner in self._owners.items() if owner == saga_id]
            for r in held:
                self._clear_lock_internal(r)
            return held

    def _clear_lock_internal(self, resource_id: str) -> None:
        self._owners.pop(resource_id, None)
        self._expirations.pop(resource_id, None)
        task = self._heartbeats.pop(resource_id, None)
        if task:
            task.cancel()
            try:
                loop = asyncio.get_running_loop()
                if loop.is_running():
                    loop.create_task(self._await_cancelled_task(task))
            except RuntimeError:
                pass

    def owner(self, resource_id: str) -> Optional[str]:
        with self._mutex:
            now = time.monotonic()
            if resource_id in self._expirations and now > self._expirations[resource_id]:
                self._clear_lock_internal(resource_id)
            return self._owners.get(resource_id)

    def held(self) -> dict[str, str]:
        with self._mutex:
            now = time.monotonic()
            for r in list(self._expirations.keys()):
                if now > self._expirations[r]:
                    self._clear_lock_internal(r)
            return dict(self._owners)


class RedisSemanticLocks:
    """Semantic locks that actually hold across nodes.

    `SemanticLockManager` is an in-memory dict: correct in one process, and a
    false sense of safety on Kubernetes, where two pods both "acquire" the same
    account and neither learns of the other. This is the real thing.

    Three details carry the correctness:

      * ACQUIRE is `SET key token NX PX ttl` -- atomic test-and-set with an
        expiry, so a holder that is SIGKILLed releases the resource when the TTL
        lapses instead of deadlocking it forever.
      * RELEASE is a compare-and-delete Lua script, never a bare DEL. If our TTL
        already lapsed and another saga took the lock, a bare DEL would free
        *their* claim -- the classic distributed-lock bug, and a silent one.
      * RENEWAL extends the TTL while the saga is alive, so a long-running agent
        does not lose a lock mid-transaction. The TTL is therefore a
        crash-detector, not a deadline on the work.

    Requires `pip install agent-saga[redis]`.
    """

    distributed = True

    # KEYS[1] = lock key, ARGV[1] = our owner token.
    _RELEASE_LUA = """
    if redis.call('get', KEYS[1]) == ARGV[1] then
        return redis.call('del', KEYS[1])
    else
        return 0
    end
    """

    _RENEW_LUA = """
    if redis.call('get', KEYS[1]) == ARGV[1] then
        return redis.call('pexpire', KEYS[1], ARGV[2])
    else
        return 0
    end
    """

    def __init__(
        self,
        url: str = "redis://localhost:6379/0",
        *,
        client: Any = None,
        key_prefix: str = "agent-saga:semlock:",
        ttl_ms: int = 30_000,
        renew_interval: Optional[float] = None,
    ):
        self.url = url
        self.key_prefix = key_prefix
        self.ttl_ms = ttl_ms
        self.renew_interval = renew_interval or ((ttl_ms / 1000.0) / 2.0)
        self._client = client
        self._owns_client = client is None
        self._held: dict[str, str] = {}      # resource_id -> saga_id, this node
        self._heartbeats: dict[str, asyncio.Task] = {}  # resource_id -> heartbeat task

        if client is None:
            try:
                import redis.asyncio  # noqa: F401
            except ImportError as exc:
                raise ImportError(
                    "RedisSemanticLocks needs the 'redis' package.\n"
                    "    pip install agent-saga[redis]"
                ) from exc

    def _key(self, resource_id: str) -> str:
        return f"{self.key_prefix}{resource_id}"

    async def _conn(self):
        if self._client is None:
            from redis.asyncio import Redis

            self._client = Redis.from_url(self.url, decode_responses=True)
        return self._client

    def try_acquire(self, resource_id: str, saga_id: str) -> bool:
        """Not available on a distributed backend: the check is a network round
        trip and cannot be done synchronously.

        Raised rather than silently returning True, because a lock that lies is
        worse than no lock at all.
        """
        raise RuntimeError(
            "RedisSemanticLocks cannot acquire synchronously (it is a network "
            "call). Use `await ctx.acquire_semantic_lock(resource_id)` before "
            "registering the resource, and pass lock=False to tentative()."
        )

    async def acquire(self, resource_id: str, saga_id: str, *,
                      timeout: float = 5.0, poll: float = 0.05) -> None:
        conn = await self._conn()
        key = self._key(resource_id)
        deadline = time.monotonic() + timeout

        while True:
            # NX+PX: atomic, and self-expiring so a dead holder cannot deadlock.
            acquired = await conn.set(key, saga_id, nx=True, px=self.ttl_ms)
            if acquired:
                self._held[resource_id] = saga_id
                self._start_heartbeat(resource_id, saga_id)
                return

            current = await conn.get(key)
            if current == saga_id:            # re-entrant for the same saga
                self._held[resource_id] = saga_id
                self._start_heartbeat(resource_id, saga_id)
                return
            if timeout <= 0 or time.monotonic() >= deadline:
                raise LockAcquisitionTimeoutError(resource_id, current or "?", saga_id, timeout)
            await asyncio.sleep(poll)

    def _start_heartbeat(self, resource_id: str, saga_id: str) -> None:
        old_task = self._heartbeats.pop(resource_id, None)
        if old_task:
            old_task.cancel()
            try:
                loop = asyncio.get_running_loop()
                if loop.is_running():
                    loop.create_task(self._await_cancelled_task(old_task))
            except RuntimeError:
                pass

        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(self._heartbeat_loop(resource_id, saga_id))
            self._heartbeats[resource_id] = task
        except RuntimeError:
            pass

    async def _heartbeat_loop(self, resource_id: str, saga_id: str) -> None:
        try:
            while True:
                await asyncio.sleep(self.renew_interval)
                conn = await self._conn()
                renewed = await conn.eval(self._RENEW_LUA, 1,
                                          self._key(resource_id), saga_id,
                                          str(self.ttl_ms))
                if not renewed:
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _await_cancelled_task(self, task: asyncio.Task) -> None:
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def release(self, resource_id: str, saga_id: str) -> bool:
        conn = await self._conn()
        # Compare-and-delete: never free a claim we no longer own.
        freed = await conn.eval(self._RELEASE_LUA, 1, self._key(resource_id), saga_id)
        self._held.pop(resource_id, None)
        task = self._heartbeats.pop(resource_id, None)
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        return bool(freed)

    async def release_all(self, saga_id: str) -> list[str]:
        held = [r for r, owner in self._held.items() if owner == saga_id]
        for resource_id in held:
            await self.release(resource_id, saga_id)
        return held

    async def owner(self, resource_id: str) -> Optional[str]:
        conn = await self._conn()
        return await conn.get(self._key(resource_id))

    async def close(self) -> None:
        tasks = list(self._heartbeats.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._heartbeats.clear()

        if self._client is not None and self._owns_client:
            close = getattr(self._client, "aclose", None) or getattr(self._client, "close", None)
            if close is not None:
                await close()
        self._client = None


_SEMANTIC_LOCKS: Any = SemanticLockManager()


def get_semantic_locks() -> SemanticLockManager:
    return _SEMANTIC_LOCKS


def set_semantic_locks(manager: SemanticLockManager) -> None:
    """Inject a shared implementation for multi-node deployments."""
    global _SEMANTIC_LOCKS
    _SEMANTIC_LOCKS = manager


__all__ = ["RecoveryLock", "FileLock", "InProcessLock",
           "SemanticLockManager", "RedisSemanticLocks",
           "SemanticLockConflictError", "LockAcquisitionTimeoutError",
           "get_semantic_locks", "set_semantic_locks"]
