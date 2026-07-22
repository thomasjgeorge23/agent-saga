"""Multi-Agent Cross-Domain Entanglement Matrix.

Binds heterogeneous framework agent execution graphs into a unified atomic
distributed saga boundary across network nodes and thread boundaries.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .context import RollbackReport, SagaContext

logger = logging.getLogger("agent_saga.entanglement")

HEADER_ENTANGLEMENT_ID = "X-Saga-Entanglement-Id"
HEADER_PARENT_STEP = "X-Saga-Parent-Step"
HEADER_CORRELATION_ID = "X-Saga-Correlation-Id"


def get_correlation_headers(ctx: SagaContext) -> dict[str, str]:
    """Builds distributed cross-process HTTP correlation headers for multi-agent calls."""
    return {
        HEADER_CORRELATION_ID: ctx.saga_id,
        HEADER_ENTANGLEMENT_ID: ctx.saga_id,
        HEADER_PARENT_STEP: str(len(ctx.stack)),
    }


@dataclass
class EntangledNode:
    agent_id: str
    framework: str
    context: SagaContext
    created_at: float = field(default_factory=time.time)


class EntanglementMatrix:
    """Coordinates atomic multi-agent cross-domain transactions."""

    def __init__(
        self,
        matrix_id: Optional[str] = None,
        *,
        max_nodes: int = 1000,
        ttl_seconds: Optional[float] = 3600.0,
    ):
        loop = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            pass
        now_str = str(loop.time()) if loop else str(time.time())
        self.matrix_id = matrix_id or f"matrix-{now_str}"
        self.max_nodes = max_nodes
        self.ttl_seconds = ttl_seconds
        self._nodes: dict[str, EntangledNode] = {}
        self._dependencies: dict[str, set[str]] = {}

    def prune(self) -> int:
        """Prune obsolete nodes based on max_nodes capacity and ttl_seconds."""
        now = time.time()
        pruned = 0
        if self.ttl_seconds is not None:
            expired = [
                aid for aid, n in self._nodes.items()
                if (now - n.created_at) > self.ttl_seconds
            ]
            for aid in expired:
                self._nodes.pop(aid, None)
                self._dependencies.pop(aid, None)
                pruned += 1

        if len(self._nodes) > self.max_nodes:
            overflow = len(self._nodes) - self.max_nodes
            to_remove = list(self._nodes.keys())[:overflow]
            for aid in to_remove:
                self._nodes.pop(aid, None)
                self._dependencies.pop(aid, None)
                pruned += 1

        return pruned

    def register_agent(
        self,
        agent_id: str,
        framework: str,
        context: SagaContext,
        depends_on: Optional[list[str]] = None,
    ) -> None:
        """Register an agent context into the entangled matrix."""
        self.prune()
        self._nodes[agent_id] = EntangledNode(agent_id, framework, context)
        if depends_on:
            self._dependencies[agent_id] = set(depends_on)
        logger.info("Entangled agent %s (%s) into matrix %s",
                    agent_id, framework, self.matrix_id)

    def inject_headers(self, headers: dict[str, str], parent_step: str = "") -> dict[str, str]:
        """Inject distributed correlation headers into an outgoing HTTP request/gRPC metadata dict."""
        headers[HEADER_ENTANGLEMENT_ID] = self.matrix_id
        if parent_step:
            headers[HEADER_PARENT_STEP] = parent_step
        return headers

    @classmethod
    def extract_headers(cls, headers: dict[str, str]) -> dict[str, str]:
        """Extract distributed entanglement details from incoming HTTP headers."""
        matrix_id = headers.get(HEADER_ENTANGLEMENT_ID) or headers.get(HEADER_ENTANGLEMENT_ID.lower()) or ""
        parent_step = headers.get(HEADER_PARENT_STEP) or headers.get(HEADER_PARENT_STEP.lower()) or ""
        return {"matrix_id": matrix_id, "parent_step": parent_step}

    async def abort_all(self, trigger_agent_id: str, reason: str = "") -> dict[str, RollbackReport]:
        """Cascade atomic rollback across all entangled agents in reverse dependency order."""
        logger.warning("Entanglement matrix %s abort triggered by %s: %s",
                       self.matrix_id, trigger_agent_id, reason)

        reports: dict[str, RollbackReport] = {}
        # Roll back registered agents in reverse order
        for agent_id, node in reversed(list(self._nodes.items())):
            try:
                report = await node.context.rollback()
                reports[agent_id] = report
                logger.info("Entangled agent %s rollback complete: clean=%s",
                            agent_id, report.clean)
            except Exception as exc:
                logger.error("Failed to roll back entangled agent %s: %r", agent_id, exc)

        return reports


__all__ = [
    "EntanglementMatrix",
    "EntangledNode",
    "HEADER_ENTANGLEMENT_ID",
    "HEADER_PARENT_STEP",
]
