"""agent-saga -- transactional boundaries for non-deterministic AI agents.

The core of AgentRollback. Three ideas, in order of commercial importance:

  1. Compensation is typed (REVERSIBLE / COMPENSABLE / IRREVERSIBLE). "Undo"
     is not one thing.
  2. The pre-flight gate refuses uncompensable actions *before* they happen.
  3. Compensations are derived at runtime from the forward call's result,
     because the agent -- not a developer at authoring time -- chose the action.
"""

from .context import RollbackReport, SagaAborted, SagaContext
from .decorator import current_saga, saga, saga_scope, tool
from .gate import (
    Decision,
    GateContext,
    PreFlightGate,
    PreFlightViolation,
    Rule,
    Verdict,
    arg_exceeds,
    semantics_is,
)
from .recovery import (
    DanglingSaga,
    DanglingStep,
    RecoveryDaemon,
    RecoveryOutcome,
    Resolution,
    parse_wal,
    recovery_token,
)
from .registry import compensator, registered, resolve
from .semantics import ActionSemantics, Compensation, SagaStep, StepState
from .durable import (
    FileSnapshotStore,
    SnapshotStore,
    StaleFile,
    get_snapshot_store,
    restore_file,
    set_snapshot_store,
    snapshot_file,
)
from .encryption import (
    EncryptedRecordError,
    FernetEncryptor,
    WALEncryptor,
    generate_key,
    get_wal_encryptor,
    set_wal_encryptor,
)
from .gc import GCReport, SnapshotGC
from .locks import FileLock, InProcessLock, RecoveryLock
from .observability import (
    CorrelationFilter,
    JsonFormatter,
    TextFormatter,
    configure_logging,
    current_correlation,
)
from .snapshot import (
    AttributeSnapshot,
    MappingSnapshot,
    SequenceSnapshot,
    SetSnapshot,
    SnapshotStrategy,
    auto_strategy,
    reversible,
)
from .wal import AsyncWAL, BackpressurePolicy, WALBackpressure

__version__ = "0.1.0"
__author__ = "SagaOps"

__all__ = [
    "ActionSemantics",
    "AsyncWAL",
    "BackpressurePolicy",
    "WALBackpressure",
    "Compensation",
    "DanglingSaga",
    "DanglingStep",
    "Decision",
    "RecoveryDaemon",
    "RecoveryOutcome",
    "Resolution",
    "compensator",
    "parse_wal",
    "recovery_token",
    "registered",
    "resolve",
    "GateContext",
    "PreFlightGate",
    "PreFlightViolation",
    "RollbackReport",
    "Rule",
    "SagaAborted",
    "SagaContext",
    "SagaStep",
    "StepState",
    "Verdict",
    "arg_exceeds",
    "current_saga",
    "saga",
    "saga_scope",
    "semantics_is",
    "tool",
    "AttributeSnapshot",
    "MappingSnapshot",
    "SequenceSnapshot",
    "SetSnapshot",
    "SnapshotStrategy",
    "auto_strategy",
    "reversible",
    "FileSnapshotStore",
    "SnapshotStore",
    "StaleFile",
    "get_snapshot_store",
    "restore_file",
    "set_snapshot_store",
    "snapshot_file",
    "GCReport",
    "SnapshotGC",
    "EncryptedRecordError",
    "FernetEncryptor",
    "WALEncryptor",
    "generate_key",
    "get_wal_encryptor",
    "set_wal_encryptor",
    "FileLock",
    "InProcessLock",
    "RecoveryLock",
    "CorrelationFilter",
    "JsonFormatter",
    "TextFormatter",
    "configure_logging",
    "current_correlation",
    "__version__",
]
