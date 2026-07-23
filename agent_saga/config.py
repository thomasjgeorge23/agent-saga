"""Unified SagaEngine configuration builder API.

Allows initializing all subsystems (WAL, SnapshotStore, Encryption, SemanticLocks,
Limits, Breakers, Telemetry) via a single clean call:

    from agent_saga import SagaEngine, FernetEncryptor, RedisSemanticLocks
    engine = SagaEngine.configure(
        encryption=FernetEncryptor(key="..."),
        semantic_locks=RedisSemanticLocks(),
        telemetry=True,
    )
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

from .encryption import WALEncryptor, set_wal_encryptor
from .locks import SemanticLockManager, set_semantic_locks
from .limits import set_limit_store

logger = logging.getLogger("agent_saga.config")

# A backend that wants startup reachability checked exposes one of these. Each is
# tried in order; the first that exists is called (sync or async both supported).
_HEALTH_METHODS = ("health_check", "ping", "check_connection")


class SagaConfigError(RuntimeError):
    """Raised when a SagaConfig backend fails eager startup validation."""
    pass


def _run_blocking(coro: Any) -> Any:
    """Run a coroutine to completion from sync code, whether or not an event loop
    is already running. Inside a running loop we cannot block on it, so we drive
    it on a short-lived worker thread with its own loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro)).result()


@dataclass
class SagaConfig:
    store: Any = None
    snapshot_store: Any = None
    encryption: Optional[WALEncryptor] = None
    semantic_locks: Optional[SemanticLockManager] = None
    limits: Any = None
    telemetry: bool = True
    breaker: Any = None

    def validate(self, *, check_connectivity: bool = True) -> None:
        """Fail fast on a misconfigured engine, at configure() time rather than
        halfway through the first saga.

        Three layers, cheapest first:

          1. Interface checks -- the configured objects implement the methods the
             engine will call.
          2. Encryption round-trip -- encrypt a probe and decrypt it back, so a
             broken or mismatched key ring is caught now, not when the first WAL
             record becomes unreadable.
          3. Connectivity -- any backend exposing a health method (e.g. a Redis
             store's PING) is contacted, so an unreachable host raises here.
             Disable with ``check_connectivity=False`` for offline/unit tests.
        """
        # 1. interface
        if self.encryption is not None:
            if not hasattr(self.encryption, "encrypt") or not hasattr(self.encryption, "decrypt"):
                raise SagaConfigError(
                    "Configured encryption object does not implement the "
                    "encrypt/decrypt interface")
        if self.semantic_locks is not None:
            if not hasattr(self.semantic_locks, "acquire") or not hasattr(self.semantic_locks, "release"):
                raise SagaConfigError(
                    "Configured semantic_locks object does not implement the "
                    "acquire/release interface")

        # 2. encryption round-trip
        if self.encryption is not None:
            self._validate_encryption()

        # 3. connectivity
        if check_connectivity:
            for name in ("store", "snapshot_store", "semantic_locks", "limits", "breaker"):
                backend = getattr(self, name)
                if backend is not None:
                    self._probe_backend(name, backend)

    def _validate_encryption(self) -> None:
        probe = b"agent-saga-config-probe"
        try:
            roundtripped = self.encryption.decrypt(self.encryption.encrypt(probe))
        except Exception as exc:
            raise SagaConfigError(
                f"Configured encryption failed a round-trip probe: {exc!r}. "
                f"Check the key (or key ring) is valid.") from exc
        if roundtripped != probe:
            raise SagaConfigError(
                "Configured encryption did not round-trip: decrypt(encrypt(x)) "
                "!= x. The key ring cannot read what it writes.")

    def _probe_backend(self, name: str, backend: Any) -> None:
        method = next((getattr(backend, m) for m in _HEALTH_METHODS if hasattr(backend, m)), None)
        if method is None:
            return
        try:
            result = method()
            if inspect.isawaitable(result):
                _run_blocking(result)
        except SagaConfigError:
            raise
        except Exception as exc:
            raise SagaConfigError(
                f"Configured {name} failed its startup health check: {exc!r}. "
                f"The backend is unreachable or misconfigured.") from exc

    def apply(self, *, check_connectivity: bool = True) -> None:
        self.validate(check_connectivity=check_connectivity)
        if self.encryption is not None:
            set_wal_encryptor(self.encryption)
        if self.semantic_locks is not None:
            set_semantic_locks(self.semantic_locks)
        if self.limits is not None:
            set_limit_store(self.limits)


class SagaEngine:
    """Unified entrypoint for configuring and building agent-saga subsystems."""

    @classmethod
    def configure(
        cls,
        *,
        store: Any = None,
        snapshot_store: Any = None,
        encryption: Optional[WALEncryptor] = None,
        semantic_locks: Optional[SemanticLockManager] = None,
        limits: Any = None,
        telemetry: bool = True,
        breaker: Any = None,
        check_connectivity: bool = True,
    ) -> SagaConfig:
        """Build and eagerly validate the engine. A bad encryption key or an
        unreachable Redis host raises SagaConfigError here, at startup. Pass
        ``check_connectivity=False`` to skip the network probes (unit tests,
        offline bring-up)."""
        cfg = SagaConfig(
            store=store,
            snapshot_store=snapshot_store,
            encryption=encryption,
            semantic_locks=semantic_locks,
            limits=limits,
            telemetry=telemetry,
            breaker=breaker,
        )
        cfg.apply(check_connectivity=check_connectivity)
        return cfg


__all__ = ["SagaConfig", "SagaEngine", "SagaConfigError"]
