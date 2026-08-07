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
    EmbeddingRiskScorer,
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
    redact_path,
)
from .integrity import verify as verify_chain
from .chaos import ChaosConfig, ChaosEngine, ChaosInjectionError
from .breaker import (
    BreakerPolicy,
    CircuitBreaker,
    CircuitOpen,
    InProcessBreakerStore,
    get_breaker,
    set_breaker,
)
from .reconcile import (
    Finding,
    Observation,
    ReconcileReport,
    Reconciliation,
    reconciler,
)
from .ha import LeaderElection, NodeState, SagaDiagnosticSuite, WALReplicator
from .vault import ComplianceEngine, VaultTamperError, WORMVault
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
    PostgresApprovalStore,
    WebhookNotifier,
    TeamsNotifier,
    DiscordNotifier,
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
from .hallucination import (
    GroundingFact,
    HallucinationDetected,
    RealityAnchor,
    SelfCorrectingLoop,
)
from .healing import HealingPath, SelfHealingGraph
from .speculative import SpeculativeEngine, StateSnapshot
from .ai_engine import (
    ContextSanitizer,
    LoopEntropyDetector,
    SemanticOutputVerifier,
    UniversalToolAdapter,
    VerifiedOutput,
)
from .mission_critical import (
    InvariantRule,
    MissionCriticalGate,
    MissionCriticalViolation,
    TripleRedundantVerifier,
)
from .auto import patch_all
from .entanglement import EntangledNode, EntanglementMatrix
from .propagation import EntanglementPropagator
from .umip import (
    Participant,
    UMIPConformanceError,
    UMIPRegistry,
    MCPTransactionProxy,
    get_registry,
    set_registry,
)
from .mesh import (
    MergeReport,
    MerkleMeshSync,
    merge_wals,
    record_identity,
    verify_merged,
)
from .hardware import (
    ActionChallenge,
    HardwareApprovalError,
    HardwareApprovalProvider,
    MultiSigApprovalProvider,
    action_digest,
)
from .preview import PreviewPlan, PreviewSaga
from .healing import HealingPath, SelfHealedProposal, SelfHealingGraph
from .approvals import UIConfirmationArtifact
from .dag import DAGNode, DAGSaga
from .durable_memory import DurableSagaMemory
from .universal import EnhancedAIResponse, UniversalAgentEngine, enhance, patch_all
from .beast import BeastEngine, BeastExecutionSummary
from .predictive import (
    PredictiveExecutor,
    Speculation,
    SpeculationRefused,
)
from .sentinel import PredictiveSentinel
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
from .observability.langchain import LangChainSagaCallback
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
from .config import SagaEngine, SagaConfig, SagaConfigError
from .encryption import KeyRingEncryptor
from .locks import AutoLockHeartbeat
from .streaming import IncrementalCompensationTracker, streaming_step
from .observability.otlp import OTLPExporter
from .feedback import SelfHealingPromptFeedback, SelfHealingLoop, HealingOutcome
from .scheduler import DurableTimerManager, CronSagaScheduler, TimerCancelled
from .signals import SignalBus, QueryBus, get_signal_bus, get_query_bus
from .orchestrator import ChildSaga, ParallelSagaGroup
from .bpmn import BPMNExporter, BPMNImporter, BPMNNode
from .determinism import ReplayVerifier, verify_replay_determinism, DeterminismResult
from .slack_app import SlackBlockKitApp
from .tenant import TenantContext, get_current_tenant, set_current_tenant
from .cloud import SagaCloudClient
from .certify import (
    SafetyCertificate,
    SafetyFinding,
    certify_rollback_safety,
)
from .provenance import (
    MerkleAuditTree,
    DisclosureResult,
    audit_root,
    build_disclosure,
    verify_disclosure,
)
from .schemas import SchemaContractError, validate_schema
from .testing import ChaosRunner, ChaosResult, verify_saga_replay
from .agentkit import AgentKit, describe_guarantees

from ._version import __version__
__author__ = "SagaOps"

__all__ = [
    "AgentKit",
    "describe_guarantees",
    "SlackBlockKitApp",
    "TenantContext",
    "get_current_tenant",
    "set_current_tenant",
    "SagaCloudClient",
    "SafetyCertificate",
    "SafetyFinding",
    "certify_rollback_safety",
    "MerkleAuditTree",
    "DisclosureResult",
    "audit_root",
    "build_disclosure",
    "verify_disclosure",
    "SchemaContractError",
    "validate_schema",
    "ChaosRunner",
    "ChaosResult",
    "verify_saga_replay",
    "SagaEngine",
    "SagaConfig",
    "SagaConfigError",
    "KeyRingEncryptor",
    "AutoLockHeartbeat",
    "IncrementalCompensationTracker",
    "streaming_step",
    "OTLPExporter",
    "SelfHealingPromptFeedback",
    "SelfHealingLoop",
    "HealingOutcome",
    "DurableTimerManager",
    "TimerCancelled",
    "CronSagaScheduler",
    "SignalBus",
    "QueryBus",
    "get_signal_bus",
    "get_query_bus",
    "ChildSaga",
    "ParallelSagaGroup",
    "BPMNExporter",
    "BPMNImporter",
    "BPMNNode",
    "ReplayVerifier",
    "verify_replay_determinism",
    "DeterminismResult",
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
    "EmbeddingRiskScorer",
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
    "CircuitBreaker",
    "BreakerPolicy",
    "CircuitOpen",
    "InProcessBreakerStore",
    "get_breaker",
    "set_breaker",
    "reconciler",
    "Reconciliation",
    "Observation",
    "ReconcileReport",
    "Finding",
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
    "PostgresApprovalStore",
    "WebhookNotifier",
    "TeamsNotifier",
    "DiscordNotifier",
    "ConsoleNotifier",
    "ChainReport",
    "export_worm",
    "redact_record",
    "redact_where",
    "redact_path",
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
    "LangChainSagaCallback",
    "NoOpTracer",
    "__version__",
    "SagaJSONEncoder",
    "saga_dumps",
    "saga_loads",
    "saga_lifespan",
    "RetryPolicy",
    "RealityAnchor",
    "GroundingFact",
    "SelfCorrectingLoop",
    "HallucinationDetected",
    "HealingPath",
    "SelfHealingGraph",
    "SpeculativeEngine",
    "StateSnapshot",
    "EntangledNode",
    "EntanglementMatrix",
    "EntanglementPropagator",
    "UMIPRegistry",
    "Participant",
    "UMIPConformanceError",
    "merge_wals",
    "verify_merged",
    "record_identity",
    "MergeReport",
    "HardwareApprovalProvider",
    "ActionChallenge",
    "action_digest",
    "PredictiveExecutor",
    "Speculation",
    "SpeculationRefused",
    "PredictiveSentinel",
    "SemanticOutputVerifier",
    "VerifiedOutput",
    "ContextSanitizer",
    "LoopEntropyDetector",
    "UniversalToolAdapter",
    "InvariantRule",
    "MissionCriticalGate",
    "MissionCriticalViolation",
    "TripleRedundantVerifier",
    "patch_all",
]

# -- v0.4 core: IR, routing, context, and the agent loop ------------------------
from .ir import (
    Capabilities,
    ChatRequest,
    ChatResponse,
    CostClass,
    HostAdapter,
    Message,
    ToolCall,
    ToolSpec,
    Usage,
)
from .router import (
    AllHostsFailed,
    HostTimeout,
    NoCapableHost,
    Router,
    RoutingDecision,
    RoutingPolicy,
    ValidationExhausted,
)
from .context_broker import (
    ColdStore,
    ContextBroker,
    FileColdStore,
    MemoryColdStore,
    PackedContext,
    ProvenanceError,
    Span,
)
from .agent_loop import (
    AgentLoop,
    AgentLoopError,
    LoopBudgetExceeded,
    LoopDeadlineExceeded,
    LoopExhausted,
    LoopResult,
    LoopStalled,
    LoopTool,
)
from .codemod import AstTransaction, CodemodResult, ShadowTree

__all__ += [
    "AgentLoop", "AgentLoopError", "AllHostsFailed", "AstTransaction",
    "Capabilities", "ChatRequest", "ChatResponse", "CodemodResult", "ColdStore",
    "ContextBroker", "CostClass", "FileColdStore", "HostAdapter", "HostTimeout",
    "LoopBudgetExceeded", "LoopDeadlineExceeded", "LoopExhausted", "LoopResult",
    "LoopStalled", "LoopTool", "MemoryColdStore", "Message", "NoCapableHost",
    "PackedContext", "ProvenanceError", "Router", "RoutingDecision",
    "RoutingPolicy", "ShadowTree", "Span", "ToolCall", "ToolSpec", "Usage",
    "ValidationExhausted",
]

# -- grounded answers: a hallucination cannot pose as a sourced fact -------------
from .grounding import Claim, GroundedAnswer, ground

__all__ += ["Claim", "GroundedAnswer", "ground"]

# -- graph export: the rollback fork, drawn --------------------------------------
from .graph import dag_to_dot, dag_to_mermaid, wal_to_dot, wal_to_mermaid

__all__ += ["dag_to_dot", "dag_to_mermaid", "wal_to_dot", "wal_to_mermaid"]

# -- production readiness and project scaffold -----------------------------------
# `Finding` at top level already means reconcile.Finding, which was exported
# first and is what callers import today. Readiness gets a qualified alias
# rather than silently shadowing it -- matching how certify's finding is
# exported as SafetyFinding. `readiness.Finding` still works unqualified.
from .readiness import Finding as ReadinessFinding
from .readiness import ReadinessReport, audit

__all__ += ["ReadinessFinding", "ReadinessReport", "audit"]

# -- v0.5.0 enterprise capabilities ----------------------------------------------
from .multi_agent_mesh import SagaMeshCoordinator, VectorClock, CrossAgentStep
from .dashboard import DashboardServer
from .zkp import ZeroKnowledgeAuditProof, ZKProofCommitment
from .cost_gate import CostBudgetGate, CostBudgetConfig, CostBudgetExceeded

__all__ += [
    "SagaMeshCoordinator", "VectorClock", "CrossAgentStep",
    "DashboardServer",
    "ZeroKnowledgeAuditProof", "ZKProofCommitment",
    "CostBudgetGate", "CostBudgetConfig", "CostBudgetExceeded",
]

# -- declarative inverses: say what undoes a tool once ---------------------------
from .inverses import (
    InverseError,
    auto_compensation,
    call_with,
    delete_by,
    has_inverse,
    inverse_of,
    map_result,
)

__all__ += ["InverseError", "auto_compensation", "call_with", "delete_by",
            "has_inverse", "inverse_of", "map_result"]

# -- prove the rollback works at every failure point -----------------------------
from .proving import ProbeMode, RollbackProof, StepProbe, prove_rollback

__all__ += ["ProbeMode", "RollbackProof", "StepProbe", "prove_rollback"]

# -- cross-framework orchestration with verifiable coverage ----------------------
from .fleet import BoundaryRequired, CoverageReport, SagaFleet, ToolCoverage, bind_saga

__all__ += ["BoundaryRequired", "CoverageReport", "SagaFleet", "ToolCoverage",
            "bind_saga"]

# -- surgical repair: fix one step and resume, instead of undoing everything -----
from .repair import RepairBlocked, RepairSession, RetainedStep

__all__ += ["RepairBlocked", "RepairSession", "RetainedStep"]

# -- cryptographic runtime integrity guard ----------------------------------------
from ._integrity_guard import (
    EngineTamperDetected,
    SAGAOPS_ENGINE_SIGNATURE,
    generate_wal_provenance_token,
    verify_engine_integrity,
    verify_wal_provenance_token,
)

__all__ += [
    "EngineTamperDetected",
    "SAGAOPS_ENGINE_SIGNATURE",
    "generate_wal_provenance_token",
    "verify_engine_integrity",
    "verify_wal_provenance_token",
]

# -- verification-gated cascade: the cheapest tier that can be proven right ------
from .cascade import CascadeExhausted, CascadeResult, Rung, cascade, tools_must_exist

__all__ += ["CascadeExhausted", "CascadeResult", "Rung", "cascade",
            "tools_must_exist"]

# -- argument provenance: no side effect on an invented number -------------------
from .provenance_gate import (
    Provenance,
    ProvenancePolicy,
    ProvenanceViolation,
    Tagged,
    derived,
    sourced,
)
from .provenance_gate import user as user_value

__all__ += ["Provenance", "ProvenancePolicy", "ProvenanceViolation", "Tagged",
            "derived", "sourced", "user_value"]

# -- counterfactual replay: would the cheaper model have got it right? ------------
from .counterfactual import (
    Divergence,
    RecordedStep,
    ReplayVerdict,
    counterfactual_replay,
)

__all__ += ["Divergence", "RecordedStep", "ReplayVerdict", "counterfactual_replay"]

# -- the WAL as a labelled training corpus ----------------------------------------
from .corpus import Corpus, Example, Label, build_corpus

__all__ += ["Corpus", "Example", "Label", "build_corpus"]

# -- synthetic WALs: the shape of your traffic, none of your customers' data ------
from .synthetic import SYNTHETIC_FIELD, WALProfile, is_synthetic, synthesize

__all__ += ["SYNTHETIC_FIELD", "WALProfile", "is_synthetic", "synthesize"]

# -- WAL-mined failure prediction: advisory, explainable, fenced in --------------
from .risk import FailureModel, RiskAssessment, RiskFactor, require_review_above

__all__ += ["FailureModel", "RiskAssessment", "RiskFactor", "require_review_above"]

# -- bounded verification of the rollback invariants ------------------------------
from .verification import (
    INVARIANTS,
    Interleaving,
    InvariantViolation,
    VerificationReport,
    verify_rollback_invariants,
)

__all__ += ["INVARIANTS", "Interleaving", "InvariantViolation",
            "VerificationReport", "verify_rollback_invariants"]

# -- CLI-backed surfaces, also callable as library APIs ---------------------------
# `demo` is deliberately NOT imported here. It registers @compensator handlers,
# and `python -m agent_saga.demo` loads the module twice -- once as
# `agent_saga.demo` via this package import, once as `__main__` -- so the second
# registration trips the registry's duplicate guard and the crash worker dies
# with the wrong exit code. Eagerly importing a demo on every `import
# agent_saga` would be wrong regardless of that. Use `agent-saga demo`, or
# `from agent_saga.demo import run_demo`.
from .array import SagaArray, array
from .curriculum import AgentCurriculum, learn
from .future import AutonomousFutureAgent, create_future_agent
from .omni import OmniProofCertificate, OmniRealityEngine, OmniSelfHealingCortex, get_omni_engine, shield
from .ultra import UltraEngine, auto_shield
from . import auto
from . import compat
from . import integrations
from . import omni
from . import ultra

guard = saga

__all__ += [
    "AgentCurriculum",
    "AutonomousFutureAgent",
    "OmniProofCertificate",
    "OmniRealityEngine",
    "OmniSelfHealingCortex",
    "SagaArray",
    "UltraEngine",
    "array",
    "auto",
    "auto_shield",
    "compat",
    "create_future_agent",
    "get_omni_engine",
    "guard",
    "integrations",
    "learn",
    "omni",
    "shield",
    "ultra",
]
