"""Multi-Step Goal Decomposition & Directed Acyclic Graph (DAG) Saga Execution Engine.

Breaks high-level user requests into a topologically ordered DAG of sub-sagas,
giving each node independent WAL tracking, localized recovery, and state isolation.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

from .context import SagaContext, RollbackReport

logger = logging.getLogger("agent_saga.dag")


@dataclass
class DAGNode:
    """A single sub-saga node inside a Directed Acyclic Graph execution plan."""

    node_id: str
    action_fn: Callable[[SagaContext], Any]
    dependencies: List[str] = field(default_factory=list)
    description: str = ""
    status: str = "PENDING"  # PENDING, RUNNING, COMPLETED, FAILED
    result: Any = None
    error: Optional[str] = None


class DAGSaga:
    """Topological Directed Acyclic Graph (DAG) Sub-Saga Orchestrator."""

    def __init__(self, parent_ctx: Optional[SagaContext] = None, name: str = "dag_saga"):
        self.parent_ctx = parent_ctx or SagaContext(name=name)
        self.nodes: Dict[str, DAGNode] = {}
        self.name = name

    def add_node(
        self,
        node_id: str,
        action_fn: Callable[[SagaContext], Any],
        dependencies: Optional[List[str]] = None,
        description: str = "",
    ) -> DAGNode:
        """Register a sub-saga node with optional upstream dependencies."""
        if node_id in self.nodes:
            raise ValueError(f"DAGNode '{node_id}' is already registered in DAGSaga '{self.name}'")

        node = DAGNode(
            node_id=node_id,
            action_fn=action_fn,
            dependencies=dependencies or [],
            description=description,
        )
        self.nodes[node_id] = node
        return node

    def topological_sort(self) -> List[DAGNode]:
        """Order DAG nodes topologically by dependency requirements."""
        in_degree = {nid: 0 for nid in self.nodes}
        for node in self.nodes.values():
            for dep in node.dependencies:
                if dep not in self.nodes:
                    raise ValueError(f"Missing upstream dependency '{dep}' for node '{node.node_id}'")

        for node in self.nodes.values():
            for dep in node.dependencies:
                in_degree[node.node_id] += 1

        queue = [self.nodes[nid] for nid, deg in in_degree.items() if deg == 0]
        order = []

        while queue:
            node = queue.pop(0)
            order.append(node)

            for candidate in self.nodes.values():
                if node.node_id in candidate.dependencies:
                    in_degree[candidate.node_id] -= 1
                    if in_degree[candidate.node_id] == 0:
                        queue.append(candidate)

        if len(order) != len(self.nodes):
            raise ValueError(f"Cyclic dependency detected in DAGSaga '{self.name}'")

        return order

    async def execute_dag(self) -> Dict[str, Any]:
        """Execute all DAG sub-sagas in topological order with child WAL contexts."""
        ordered_nodes = self.topological_sort()
        executed_results = {}

        logger.info("Executing DAGSaga '%s' with %d sub-saga nodes", self.name, len(ordered_nodes))

        for node in ordered_nodes:
            node.status = "RUNNING"
            child_ctx = self.parent_ctx.create_child_scope(node.node_id)
            await child_ctx.begin()

            try:
                if asyncio.iscoroutinefunction(node.action_fn):
                    res = await node.action_fn(child_ctx)
                else:
                    res = node.action_fn(child_ctx)

                node.result = res
                node.status = "COMPLETED"
                executed_results[node.node_id] = res
                await child_ctx.finish(aborted=False, clean=True)
                logger.info("DAG sub-saga node '%s' completed successfully", node.node_id)
            except Exception as exc:
                node.status = "FAILED"
                node.error = repr(exc)
                logger.error("DAG sub-saga node '%s' failed: %r. Initiating localized rollback.", node.node_id, exc)
                await child_ctx.rollback()
                await child_ctx.finish(aborted=True, clean=False)
                raise RuntimeError(f"DAG sub-saga node '{node.node_id}' failed: {exc}") from exc

        return executed_results
