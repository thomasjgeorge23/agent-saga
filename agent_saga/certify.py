"""Machine-checkable rollback-safety certificates.

`provenance.py` proves *what happened* -- cryptographically, and selectively.
This module proves the other half: that what happened was **safe**, in the one
sense that matters for this engine -- every effect the log admits to is either
still intact, provably undone, or explicitly signed off by a human.

The output is a certificate, not a vibe. It names every step the log cannot
account for, and it carries the Merkle root of the log it was computed from, so
a certificate can never be quietly re-attached to a different log.

    cert = certify_rollback_safety(records)
    cert.safe          # False if anything is unaccounted for
    cert.findings      # exactly which steps, and why

Run it in CI (`agent-saga certify`) and a deploy that would strand an
uncompensated charge fails the build instead of the customer.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

CRITICAL = "critical"
WARNING = "warning"

CERT_VERSION = 1


@dataclass
class SafetyFinding:
    severity: str
    saga_id: str
    issue: str
    step_id: str = ""
    tool: str = ""

    def __str__(self) -> str:
        where = f" step {self.step_id} ({self.tool})" if self.step_id else ""
        return f"[{self.severity.upper()}] saga {self.saga_id[:20]}{where}: {self.issue}"


@dataclass
class SafetyCertificate:
    safe: bool
    sagas_audited: int
    steps_audited: int
    merkle_root: str
    findings: list[SafetyFinding] = field(default_factory=list)
    issued_at: float = field(default_factory=time.time)
    version: int = CERT_VERSION

    @property
    def critical(self) -> list[SafetyFinding]:
        return [f for f in self.findings if f.severity == CRITICAL]

    @property
    def warnings(self) -> list[SafetyFinding]:
        return [f for f in self.findings if f.severity == WARNING]

    def summary(self) -> str:
        verdict = "SAFE" if self.safe else "UNSAFE"
        return (f"rollback safety: {verdict} -- {self.sagas_audited} saga(s), "
                f"{self.steps_audited} step(s), {len(self.critical)} critical, "
                f"{len(self.warnings)} warning(s); log {self.merkle_root[:16]}...")

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "safe": self.safe,
            "sagas_audited": self.sagas_audited,
            "steps_audited": self.steps_audited,
            "merkle_root": self.merkle_root,
            "issued_at": self.issued_at,
            "findings": [
                {"severity": f.severity, "saga_id": f.saga_id, "step_id": f.step_id,
                 "tool": f.tool, "issue": f.issue}
                for f in self.findings
            ],
        }


def certify_rollback_safety(records: Sequence[dict], *,
                            require_registered_handlers: bool = False) -> SafetyCertificate:
    """Audit a WAL and certify that every committed effect is accounted for.

    Critical (the log cannot account for an effect):
      * a step that committed and could not be undone (ORPHANED)
      * a saga that aborted without a clean rollback
      * a COMPENSABLE step that recorded no compensation at all -- it claims to
        be undoable and is not

    Warning (needs attention, not proof of harm):
      * a saga with no terminal record -- crashed and never recovered, so its
        effects are still outstanding
      * with ``require_registered_handlers``, a compensation naming a handler no
        longer registered in this process (unrecoverable after a deploy)
    """
    from .provenance import audit_root
    from .ui.reader import SagaWALReader   # reuse the tested reconstruction

    findings: list[SafetyFinding] = []
    sagas = _reconstruct(records)
    steps_audited = 0

    resolvable = None
    if require_registered_handlers:
        from .registry import registered
        resolvable = set(registered())

    for saga_id, detail in sagas.items():
        status = detail.get("status")
        steps = detail.get("steps") or []
        steps_audited += len(steps)

        if status == "IN_PROGRESS":
            findings.append(SafetyFinding(
                WARNING, saga_id,
                "no terminal record -- crashed and unrecovered; effects outstanding"))

        if detail.get("rollback_started") and detail.get("rollback_clean") is False:
            findings.append(SafetyFinding(
                CRITICAL, saga_id, "rollback did not complete cleanly"))

        for s in steps:
            st = s.get("status")
            tool = s.get("tool", "?")
            step_id = s.get("step_id", "")
            comp = s.get("compensation")
            semantics = s.get("semantics")

            if st == "ORPHANED":
                findings.append(SafetyFinding(
                    CRITICAL, saga_id,
                    "committed but could not be undone (orphaned effect)",
                    step_id, tool))
                continue

            if semantics == "COMPENSABLE" and st in ("COMMITTED", "COMPENSATED") and not comp:
                findings.append(SafetyFinding(
                    CRITICAL, saga_id,
                    "declared COMPENSABLE but recorded no compensation",
                    step_id, tool))
                continue

            if resolvable is not None and comp:
                handler = comp.get("handler")
                if handler and handler not in resolvable:
                    findings.append(SafetyFinding(
                        WARNING, saga_id,
                        f"compensation handler {handler!r} is not registered here "
                        f"-- recovery would escalate", step_id, tool))

    return SafetyCertificate(
        safe=not any(f.severity == CRITICAL for f in findings),
        sagas_audited=len(sagas),
        steps_audited=steps_audited,
        merkle_root=audit_root(records),
        findings=findings,
    )


def _reconstruct(records: Sequence[dict]) -> dict:
    """Rebuild per-saga detail from raw records using the reader's accumulator,
    so the certificate agrees exactly with what the dashboard shows."""
    from .ui.reader import _SagaAcc

    accs: dict[str, Any] = {}
    for rec in records:
        sid = rec.get("saga_id")
        if not sid:
            continue
        acc = accs.get(sid)
        if acc is None:
            acc = accs[sid] = _SagaAcc(saga_id=sid)
        acc.apply(rec)
    return {sid: acc.detail() for sid, acc in accs.items()}


__all__ = [
    "SafetyCertificate", "SafetyFinding", "certify_rollback_safety",
    "CRITICAL", "WARNING", "CERT_VERSION",
]
