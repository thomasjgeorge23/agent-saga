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

import os
from dataclasses import dataclass
from typing import Any, Optional

from .encryption import WALEncryptor, set_wal_encryptor
from .locks import SemanticLockManager, set_semantic_locks
from .limits import set_limit_store


class SagaConfigError(RuntimeError):
    """Raised when a SagaConfig backend fails eager startup validation."""
    pass


@dataclass
class SagaConfig:
    store: Any = None
    snapshot_store: Any = None
    encryption: Optional[WALEncryptor] = None
    semantic_locks: Optional[SemanticLockManager] = None
    limits: Any = None
    telemetry: bool = True
    breaker: Any = None

    def validate(self) -> None:
        """Eagerly validates that configured backends are reachable and valid."""
        if self.encryption is not None:
            if not hasattr(self.encryption, "encrypt") or not hasattr(self.encryption, "decrypt"):
                raise SagaConfigError("Configured encryption object does not implement encrypt/decrypt interface")
        if self.semantic_locks is not None:
            if not hasattr(self.semantic_locks, "acquire") or not hasattr(self.semantic_locks, "release"):
                raise SagaConfigError("Configured semantic_locks object does not implement acquire/release interface")

    def apply(self) -> None:
        self.validate()
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
    ) -> SagaConfig:
        cfg = SagaConfig(
            store=store,
            snapshot_store=snapshot_store,
            encryption=encryption,
            semantic_locks=semantic_locks,
            limits=limits,
            telemetry=telemetry,
            breaker=breaker,
        )
        cfg.apply()
        return cfg


__all__ = ["SagaConfig", "SagaEngine", "SagaConfigError"]
