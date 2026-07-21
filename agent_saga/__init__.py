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
from .retry import RetryPolicy
from .gate import (
    Decision,
    GateContext,
    PreFlightGate,
    PreFlightViolation,
    Rule,
    Verdict,
    arg_exceeds,
    semantics_is,
    tool_is,
)
from .limits import (
    BudgetLimit,
    InProcessLimitStore,
    LimitExceeded,
    LimitMisconfigured,
    RateLimit,
    RedisLimitStore,
    by_arg,
    by_tool,
    combine,
    get_limit_store,
    set_limit_store,
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
from .integrity import (
    ChainReport,
    export_worm,
    redact_record,
    redact_where,
)
from .integrity import verify as verify_chain
from .killswitch import (
    FileSwitchStore,
    Halted,
    KillSwitch,
    RedisSwitchStore,
    get_kill_switch,
    set_kill_switch,
)
from .approvals import (
    ApprovalGateway,
    ApprovalPolicy,
    ApprovalRequest,
    ConsoleNotifier,
    EscalationLevel,
    FileApprovalStore,
    RedisApprovalStore,
    WebhookNotifier,
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
from .executors import (
    BoundedExecutor,
    configure_tool_executor,
    get_tool_executor,
    set_tool_executor,
    tool_executor_stats,
)
from .gc import GCReport, SnapshotGC
from .idempotency import IdempotencyManager
from .ledger import FileLedger, InMemoryLedger, RecoveryLedger
from .locks import (
    FileLock,
    InProcessLock,
    RecoveryLock,
    SemanticLockConflictError,
    LockAcquisitionTimeoutError,
    RedisSemanticLocks,
    SemanticLockManager,
    get_semantic_locks,
    set_semantic_locks,
)
from .patterns import TentativeResource, TentativeStatus, tentative
from .observability import (
    CorrelationFilter,
    JsonFormatter,
    TextFormatter,
    configure_logging,
    current_correlation,
)
from .observability.otel import (
    NoOpTracer,
    SagaTracer,
    get_tracer,
    setup_telemetry,
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
from .wal import (
    AsyncWAL,
    BackpressurePolicy,
    BaseWAL,
    FileWAL,
    WALBackpressure,
    WALStalled,
)
from .serialization import SagaJSONEncoder, dumps as saga_dumps, loads as saga_loads
from .frameworks import saga_lifespan

__version__ = "0.1.6"
__author__ = "SagaOps"

__all__ = [
    "ActionSemantics",
    "AsyncWAL",
    "BackpressurePolicy",
    "BaseWAL",
    "FileWAL",
    "WALBackpressure",
    "WALStalled",
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
    "tool_is",
    "verify_chain",
    "KillSwitch",
    "Halted",
    "FileSwitchStore",
    "RedisSwitchStore",
    "get_kill_switch",
    "set_kill_switch",
    "ApprovalGateway",
    "ApprovalPolicy",
    "ApprovalRequest",
    "EscalationLevel",
    "FileApprovalStore",
    "RedisApprovalStore",
    "WebhookNotifier",
    "ConsoleNotifier",
    "ChainReport",
    "export_worm",
    "redact_record",
    "redact_where",
    "BudgetLimit",
    "RateLimit",
    "LimitExceeded",
    "LimitMisconfigured",
    "InProcessLimitStore",
    "RedisLimitStore",
    "by_arg",
    "by_tool",
    "combine",
    "get_limit_store",
    "set_limit_store",
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
    "IdempotencyManager",
    "BoundedExecutor",
    "configure_tool_executor",
    "get_tool_executor",
    "set_tool_executor",
    "tool_executor_stats",
    "EncryptedRecordError",
    "FernetEncryptor",
    "WALEncryptor",
    "generate_key",
    "get_wal_encryptor",
    "set_wal_encryptor",
    "FileLock",
    "InProcessLock",
    "RecoveryLock",
    "SemanticLockManager",
    "RedisSemanticLocks",
    "SemanticLockConflictError",
    "LockAcquisitionTimeoutError",
    "get_semantic_locks",
    "set_semantic_locks",
    "FileLedger",
    "InMemoryLedger",
    "RecoveryLedger",
    "TentativeResource",
    "TentativeStatus",
    "tentative",
    "CorrelationFilter",
    "JsonFormatter",
    "TextFormatter",
    "configure_logging",
    "current_correlation",
    "setup_telemetry",
    "get_tracer",
    "SagaTracer",
    "NoOpTracer",
    "__version__",
    "SagaJSONEncoder",
    "saga_dumps",
    "saga_loads",
    "saga_lifespan",
    "RetryPolicy",
]
