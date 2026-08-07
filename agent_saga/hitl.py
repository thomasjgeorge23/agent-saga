"""`agent_saga/hitl.py` -- Autonomous HITL Recovery Portal & Orphaned State Manager.

Manages external gateway failure fallback when compensations fail due to downstream API outages.
Classifies transactions into NEEDS_HUMAN or ORPHANED states and queues them into
an interactive human-in-the-loop audit trail.

Published & Maintained by: SAGAOPS Enterprise
Founder & Owner: Thomas J George (thomasjgeorge23@gmail.com)
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("agent_saga.hitl")


class OrphanedTransaction:
    """Represents a transaction whose compensation failed due to third-party outages."""

    def __init__(self, saga_id: str, step_name: str, error_reason: str, payload: Dict[str, Any]) -> None:
        self.saga_id = saga_id
        self.step_name = step_name
        self.error_reason = error_reason
        self.payload = payload
        self.status = "NEEDS_HUMAN"
        self.created_at = time.time()

    def resolve_manually(self, engineer_notes: str) -> None:
        self.status = "RESOLVED_MANUALLY"
        logger.info("Orphaned tx '%s' step '%s' resolved manually: %s", self.saga_id, self.step_name, engineer_notes)


class HITLManager:
    """Human-In-The-Loop Manager for queueing and resolving orphaned agentic transactions."""

    def __init__(self) -> None:
        self._orphaned_queue: List[OrphanedTransaction] = []

    def record_orphaned(self, saga_id: str, step_name: str, error_reason: str, payload: Dict[str, Any]) -> OrphanedTransaction:
        tx = OrphanedTransaction(saga_id, step_name, error_reason, payload)
        self._orphaned_queue.append(tx)
        logger.warning("🚨 [HITL ALERT] Saga '%s' step '%s' classified as NEEDS_HUMAN. Reason: %s", saga_id, step_name, error_reason)
        return tx

    def pending_orphaned(self) -> List[OrphanedTransaction]:
        return [tx for tx in self._orphaned_queue if tx.status == "NEEDS_HUMAN"]

    def resolve(self, saga_id: str, step_name: str, notes: str) -> bool:
        for tx in self._orphaned_queue:
            if tx.saga_id == saga_id and tx.step_name == step_name:
                tx.resolve_manually(notes)
                return True
        return False


_global_hitl_manager = HITLManager()


def get_hitl_manager() -> HITLManager:
    return _global_hitl_manager


def human_in_the_loop(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator marking a step as requiring explicit HITL approval if compensations fail."""
    func.__saga_hitl__ = True  # type: ignore
    return func


__all__ = ["HITLManager", "OrphanedTransaction", "get_hitl_manager", "human_in_the_loop"]
