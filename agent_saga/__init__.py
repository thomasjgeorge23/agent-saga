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
from .wal import AsyncWAL

__version__ = "0.1.0"
__author__ = "Avertis Systems"

__all__ = [
    "ActionSemantics",
    "AsyncWAL",
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
    "__version__",
]
