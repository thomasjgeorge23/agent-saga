"""Invariant gate + N-of-M consensus verification for high-stakes tool payloads.

Two fail-closed veto points that run before a side effect is dispatched:

`MissionCriticalGate` evaluates a caller-supplied list of `InvariantRule`
predicates against the payload and raises `MissionCriticalViolation` on the
first rule that fails -- or that throws. A rule that errors is treated as a
failed rule, not a skipped one, because an invariant that never ran provides
no evidence the payload is safe.

`TripleRedundantVerifier` runs three independent checks (structural, boundary
range, ledger consistency) and authorizes only on unanimous 3/3 consensus. A
checker that raises counts as a dissenting vote, for the same reason.

Both are veto mechanisms, nothing more: they can only block payloads the
supplied rules and checkers actually catch, and they make no reliability
guarantee about the system around them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Sequence

logger = logging.getLogger("agent_saga.mission_critical")


class MissionCriticalViolation(Exception):
    """Raised when a mission-critical safety or financial boundary is breached."""

    def __init__(self, reason: str, details: dict):
        self.reason = reason
        self.details = details
        super().__init__(f"MISSION-CRITICAL SAFETY VIOLATION: {reason}. Details: {details}")


@dataclass
class InvariantRule:
    name: str
    check_fn: Callable[[dict], bool]
    error_message: str


class MissionCriticalGate:
    """Zero-Tolerance Invariant Gate for Medical Dosage, Banking Settlement, and Aerospace Flight Control."""

    def __init__(self, rules: Sequence[InvariantRule]):
        self.rules = list(rules)

    def validate_preflight(self, payload: dict) -> None:
        """Assert 100% compliance across all safety rules before side effect dispatch."""
        for rule in self.rules:
            try:
                if not rule.check_fn(payload):
                    logger.critical("MISSION-CRITICAL BREACH: Rule '%s' failed on payload %r",
                                    rule.name, payload)
                    raise MissionCriticalViolation(
                        reason=f"Rule '{rule.name}' failed: {rule.error_message}",
                        details={"rule": rule.name, "payload": payload},
                    )
            except Exception as exc:
                if isinstance(exc, MissionCriticalViolation):
                    raise
                logger.critical("Rule execution error in '%s': %r", rule.name, exc)
                raise MissionCriticalViolation(
                    reason=f"Rule '{rule.name}' threw execution error: {exc!r}",
                    details={"rule": rule.name, "error": repr(exc)},
                )


class TripleRedundantVerifier:
    """Executes 3 independent verification passes (Structural, Boundary Range, Ledger Consistency).

    Requires unanimous 3/3 consensus before authorizing high-risk transactions.
    """

    def __init__(
        self,
        structural_checker: Callable[[dict], bool],
        boundary_checker: Callable[[dict], bool],
        ledger_checker: Callable[[dict], bool],
    ):
        self.structural_checker = structural_checker
        self.boundary_checker = boundary_checker
        self.ledger_checker = ledger_checker

    def verify_consensus(self, payload: dict) -> tuple[bool, str, dict[str, bool]]:
        results = {}

        try:
            results["structural"] = bool(self.structural_checker(payload))
        except Exception:
            results["structural"] = False

        try:
            results["boundary"] = bool(self.boundary_checker(payload))
        except Exception:
            results["boundary"] = False

        try:
            results["ledger"] = bool(self.ledger_checker(payload))
        except Exception:
            results["ledger"] = False

        unanimous = all(results.values())
        if unanimous:
            return True, "3/3 Triple Redundant Consensus Verified", results

        failed_passes = [k for k, v in results.items() if not v]
        reason = f"Consensus Failed (Failed passes: {', '.join(failed_passes)})"
        logger.critical("TRIPLE REDUNDANT VERIFIER DENIAL: %s. Results: %r", reason, results)
        return False, reason, results


__all__ = [
    "InvariantRule",
    "MissionCriticalGate",
    "MissionCriticalViolation",
    "TripleRedundantVerifier",
]
