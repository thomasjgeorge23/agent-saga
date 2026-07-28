"""Production readiness: the postures that lose effects, named before they do.

`config.py` answers "is this configuration valid?" and `certify.py` answers
"was that log safe?". Neither answers the question a team actually has the
week before launch: **is this deployment shaped like one that can keep its
promises?** A saga engine can be perfectly configured and still be one
`kill -9` away from an orphaned charge, because the dangerous choices are not
errors -- they are defaults that were fine on a laptop.

Three of them cost real money in production, and all three are silent:

  * A **file-backed WAL under more than one replica.** Pod A's log is not on
    pod B's disk, so pod B's recovery daemon cannot see pod A's orphans. On a
    laptop this is invisible; behind a load balancer it means half your
    unfinished sagas have no one who can finish them.
  * **In-process-only compensations.** `compensate=lambda: refund(id)` rolls
    back beautifully until the process dies, at which point the closure dies
    with it and no daemon anywhere can run it. Only a registry-named handler
    with JSON kwargs survives (see `registry.py`).
  * **Silently dropped WAL records.** Under `DROP_SILENT` backpressure a
    record that never lands is a side effect that can never be recovered --
    the engine's one unrecoverable failure mode, chosen by a config flag.

Every check here reads actual runtime state -- the live WAL, the compensation
registry, the tracer, the encryptor -- and never guesses. A check that cannot
determine an answer says so rather than reporting a pass, because a readiness
report that flatters a deployment is the same class of lie as a rollback that
reports clean when it was partial.

    agent-saga doctor              # human report, exit 1 on blockers
    agent-saga doctor --strict     # exit 1 on risks too, for a CI gate

or in-process, e.g. behind a `/readyz` endpoint:

    from agent_saga.readiness import audit
    report = audit(wal=app.state.saga_wal)
    report.production_ready        # False if any blocker was found
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "BLOCKER",
    "Finding",
    "NOTE",
    "RISK",
    "ReadinessReport",
    "audit",
]

BLOCKER = "blocker"
"""A posture that is known to lose or hide effects. Not a preference."""

RISK = "risk"
"""Works today; a realistic, specific failure mode is uncovered."""

NOTE = "note"
"""Hardening you may have deliberately declined. Listed, never nagged about."""

_ORDER = {BLOCKER: 0, RISK: 1, NOTE: 2}


@dataclass(frozen=True)
class Finding:
    """One posture worth knowing about. `code` is stable across releases so a
    team can suppress or track a specific finding without string-matching
    prose."""

    severity: str
    code: str
    summary: str
    detail: str
    fix: str

    def describe(self) -> dict:
        return {"severity": self.severity, "code": self.code,
                "summary": self.summary, "detail": self.detail, "fix": self.fix}


@dataclass(frozen=True)
class ReadinessReport:
    findings: Tuple[Finding, ...]
    checked: Tuple[str, ...]
    """Every check that ran, by code -- so a clean report is evidence of work
    done, not evidence of a checker that quietly skipped everything."""

    @property
    def blockers(self) -> Tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == BLOCKER)

    @property
    def risks(self) -> Tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == RISK)

    @property
    def production_ready(self) -> bool:
        """No blockers. Risks are deliberately allowed through: some are
        legitimate for a single-replica deployment, and a gate that refused
        everything would just get switched off."""
        return not self.blockers

    def describe(self) -> dict:
        return {
            "production_ready": self.production_ready,
            "checked": list(self.checked),
            "counts": {s: sum(1 for f in self.findings if f.severity == s)
                       for s in (BLOCKER, RISK, NOTE)},
            "findings": [f.describe() for f in self.findings],
        }

    def format_text(self) -> str:
        lines = [f"agent-saga readiness: {len(self.checked)} checks run"]
        if not self.findings:
            lines.append("\n  Nothing to report. Every check passed.")
            return "\n".join(lines)
        marks = {BLOCKER: "BLOCKER", RISK: "RISK   ", NOTE: "NOTE   "}
        for finding in self.findings:
            lines.append(f"\n  [{marks[finding.severity]}] {finding.code}: {finding.summary}")
            lines.append(f"            {finding.detail}")
            lines.append(f"       fix: {finding.fix}")
        lines.append(
            f"\n  {len(self.blockers)} blocker(s), {len(self.risks)} risk(s), "
            f"{len(self.findings) - len(self.blockers) - len(self.risks)} note(s)")
        return "\n".join(lines)


def audit(*, wal: Any = None, replicas: Optional[int] = None) -> ReadinessReport:
    """Inspect the live engine and report its production posture.

    `wal` defaults to the process-wide default WAL. `replicas` lets a caller
    state how many processes share this deployment; when given as >1 the
    shared-log finding is promoted from a risk to a blocker, because at that
    point it is not a hypothetical -- it is arithmetic.
    """
    findings: List[Finding] = []
    checked: List[str] = []

    resolved = wal if wal is not None else _default_wal()
    _check_wal(resolved, replicas, findings, checked)
    _check_registry(findings, checked)
    _check_encryption(findings, checked)
    _check_telemetry(findings, checked)
    _check_locks(replicas, findings, checked)

    findings.sort(key=lambda f: (_ORDER[f.severity], f.code))
    return ReadinessReport(findings=tuple(findings), checked=tuple(checked))


# -- checks --------------------------------------------------------------------

def _check_wal(wal: Any, replicas: Optional[int],
               findings: List[Finding], checked: List[str]) -> None:
    checked.append("wal-configured")
    if wal is None:
        findings.append(Finding(
            RISK, "wal-configured",
            "no default WAL is set in this process",
            "Sagas started without an explicit `wal=` build a throwaway "
            "in-memory-backed log, so nothing survives a restart and no "
            "recovery daemon can resolve orphans.",
            "set_default_wal(...) at startup, or use saga_lifespan() / "
            "SagaEngine.configure() to install one."))
        return

    name = type(wal).__name__

    checked.append("wal-shared")
    if name in ("FileWAL", "MmapWAL", "AsyncWAL"):
        multi = replicas is not None and replicas > 1
        findings.append(Finding(
            BLOCKER if multi else RISK, "wal-shared",
            f"{name} is local to this process's filesystem",
            (f"{replicas} replicas were declared, so this is not hypothetical: "
             f"each pod writes a log the others cannot read, and a pod that dies "
             f"leaves orphans no surviving pod can recover."
             if multi else
             "If a second replica is ever added, each will write a log the "
             "others cannot read, and orphans from a dead pod will have no "
             "recovery daemon that can see them."),
            "Use PostgresWAL or RedisWAL for any deployment with more than one "
            "process (pip install agent-saga[postgres] or [redis])."))

    checked.append("wal-backpressure")
    policy = getattr(getattr(wal, "backpressure", None), "value", None)
    if policy == "DROP_SILENT":
        findings.append(Finding(
            BLOCKER, "wal-backpressure",
            "backpressure is DROP_SILENT: records may be discarded",
            "A dropped STEP_INTENT is a side effect with no record, which is "
            "the one failure this engine cannot recover from. The buffer "
            "filling becomes silent data loss instead of a loud error.",
            "Use BackpressurePolicy.RAISE (the default) or BLOCK."))

    checked.append("wal-dropped")
    dropped = getattr(wal, "dropped", 0) or 0
    if dropped:
        findings.append(Finding(
            BLOCKER, "wal-dropped",
            f"{dropped} record(s) have already been dropped",
            "Rollback history is incomplete for this process. Some effects "
            "have no durable intent, so no daemon can compensate them.",
            "Investigate the flush path, raise max_buffer, and switch off "
            "DROP_SILENT."))

    checked.append("wal-chain")
    if getattr(wal, "chain", True) is False:
        findings.append(Finding(
            RISK, "wal-chain",
            "hash chaining is disabled: the log is not tamper-evident",
            "`agent-saga verify` cannot prove this log was not edited, and "
            "provenance proofs and rollback certificates lose their binding.",
            "Leave chain=True (the default); the cost is one SHA-256 per "
            "record, off the caller's thread."))

    checked.append("wal-barrier-timeout")
    if "barrier_timeout" in dir(wal) and getattr(wal, "barrier_timeout") is None:
        findings.append(Finding(
            RISK, "wal-barrier-timeout",
            "durability fences wait forever",
            "If the volume fills or the device wedges, barrier() never "
            "returns: the agent hangs with no error and no rollback, instead "
            "of failing before the side effect.",
            "Leave barrier_timeout at its default (30s) rather than None."))


def _check_registry(findings: List[Finding], checked: List[str]) -> None:
    checked.append("compensations-recoverable")
    try:
        from .registry import registered
        names = registered()
    except Exception:                      # a broken import must not fail the audit
        return
    if not names:
        findings.append(Finding(
            RISK, "compensations-recoverable",
            "no compensation handlers are registered",
            "Every compensation in this process is an in-process closure. "
            "Those roll back correctly on a normal exception and are lost "
            "entirely if the process dies -- saga-recoveryd has only the WAL "
            "to work from, and a closure is not in it.",
            "Register the inverse of each durable effect with @compensator("
            "\"name\") and pass handler=\"name\" plus JSON-serialisable kwargs."))


def _check_encryption(findings: List[Finding], checked: List[str]) -> None:
    checked.append("wal-encryption")
    try:
        from .encryption import get_wal_encryptor
        encryptor = get_wal_encryptor()
    except Exception:
        return
    if encryptor is None:
        findings.append(Finding(
            NOTE, "wal-encryption",
            "WAL payloads are written in cleartext",
            "Tool arguments and results land on disk as-is. That is often "
            "fine, and is a real problem if any of them carry personal or "
            "payment data.",
            "Set AGENT_SAGA_WAL_KEY or pass an encryptor (pip install "
            "agent-saga[encryption]); or redact at the source."))


def _check_telemetry(findings: List[Finding], checked: List[str]) -> None:
    checked.append("telemetry")
    try:
        from .observability.otel import get_tracer
        tracer = get_tracer()
    except Exception:
        return
    if type(tracer).__name__ == "NoOpTracer":
        findings.append(Finding(
            NOTE, "telemetry",
            "no tracer is installed: saga spans go nowhere",
            "The WAL still records everything; this only affects live "
            "dashboards and latency attribution.",
            "setup_telemetry() (pip install agent-saga[otel])."))


def _check_locks(replicas: Optional[int],
                 findings: List[Finding], checked: List[str]) -> None:
    checked.append("locks-distributed")
    try:
        from .locks import get_semantic_locks
        locks = get_semantic_locks()
    except Exception:
        return
    if locks is None:
        return
    if type(locks).__name__ in ("InProcessLock", "FileLock") and (replicas or 1) > 1:
        findings.append(Finding(
            RISK, "locks-distributed",
            f"{type(locks).__name__} cannot isolate resources across processes",
            f"With {replicas} replicas, two agents can mutate the same "
            f"external resource concurrently: the lock only holds inside one "
            f"process.",
            "Use RedisSemanticLocks (pip install agent-saga[redis])."))


def _default_wal() -> Any:
    try:
        from . import decorator
        return getattr(decorator, "_DEFAULT_WAL", None)
    except Exception:
        return None
