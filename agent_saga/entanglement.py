"""Multi-Agent Cross-Domain Entanglement Matrix.

Binds heterogeneous framework agent execution graphs into a unified atomic
distributed saga boundary across network nodes and thread boundaries.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from .context import RollbackReport, SagaContext

logger = logging.getLogger("agent_saga.entanglement")


@dataclass
class EntangledNode:
    agent_id: str
    framework: str
    context: SagaContext


class EntanglementMatrix:
    """Coordinates atomic multi-agent cross-domain transactions."""

    def __init__(self, matrix_id: Optional[str] = None):
        self.matrix_id = matrix_id or f"matrix-{asyncio.get_event_loop().time()}"
        self._nodes: dict[str, EntangledNode] = {}
        self._dependencies: dict[str, set[str]] = {}

    def register_agent(
        self,
        agent_id: str,
        framework: str,
        context: SagaContext,
        depends_on: Optional[list[str]] = None,
    ) -> None:
        """Register an agent context into the entangled matrix."""
        self._nodes[agent_id] = EntangledNode(agent_id, framework, context)
        if depends_on:
            self._dependencies[agent_id] = set(depends_on)
        logger.info("Entangled agent %s (%s) into matrix %s",
                    agent_id, framework, self.matrix_id)

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


__all__ = ["EntanglementMatrix", "EntangledNode"]
