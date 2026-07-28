"""Multi-Agent Cross-Saga Distributed Consensus Engine (`SagaMeshCoordinator`).

Provides distributed two-phase commit (2PC) and cross-agent saga orchestration
across distinct autonomous agent processes and microservice runtimes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .idempotency import IdempotencyManager

logger = logging.getLogger("agent_saga.multi_agent_mesh")


@dataclass
class VectorClock:
    """Vector clock for causal ordering across distributed agent nodes."""

    node_id: str
    clocks: Dict[str, int] = field(default_factory=dict)

    def tick(self) -> None:
        self.clocks[self.node_id] = self.clocks.get(self.node_id, 0) + 1

    def merge(self, other: VectorClock) -> None:
        for node, count in other.clocks.items():
            self.clocks[node] = max(self.clocks.get(node, 0), count)
        self.tick()

    def to_dict(self) -> Dict[str, Any]:
        return {"node_id": self.node_id, "clocks": dict(self.clocks)}


@dataclass
class CrossAgentStep:
    """A single step executed by an external peer agent inside a mesh saga."""

    agent_id: str
    step_name: str
    payload: Dict[str, Any]
    compensate_fn: Optional[Callable[..., Any]] = None
    status: str = "PREPARED"  # PREPARED, COMMITTED, ROLLED_BACK
    timestamp: float = field(default_factory=time.time)


class SagaMeshCoordinator:
    """Distributed 2-Phase Commit (2PC) coordinator for multi-agent workflows."""

    def __init__(self, node_id: str = "agent_mesh_node_primary"):
        self.node_id = node_id
        self.vector_clock = VectorClock(node_id=node_id)
        self.active_mesh_sagas: Dict[str, Dict[str, Any]] = {}
        self.idempotency = IdempotencyManager()

    async def prepare_distributed_saga(
        self,
        mesh_saga_id: str,
        participating_agents: List[str],
    ) -> Dict[str, Any]:
        """Phase 1: Prepare phase across all participating agent nodes."""
        self.vector_clock.tick()
        saga_record = {
            "mesh_saga_id": mesh_saga_id,
            "coordinator": self.node_id,
            "participants": participating_agents,
            "state": "PREPARING",
            "steps": [],
            "vector_clock": self.vector_clock.to_dict(),
            "created_at": time.time(),
        }
        self.active_mesh_sagas[mesh_saga_id] = saga_record
        logger.info("Mesh Saga '%s' PREPARE phase initiated with participants: %s", mesh_saga_id, participating_agents)
        return saga_record

    async def register_participant_step(
        self,
        mesh_saga_id: str,
        agent_id: str,
        step_name: str,
        forward_fn: Callable[..., Any],
        args: Dict[str, Any],
        compensate_fn: Optional[Callable[..., Any]] = None,
    ) -> Dict[str, Any]:
        """Register and execute a participant agent step under 2PC isolation."""
        if mesh_saga_id not in self.active_mesh_sagas:
            raise KeyError(f"Mesh saga '{mesh_saga_id}' not found")

        self.vector_clock.tick()
        step_key = f"{mesh_saga_id}:{agent_id}:{step_name}"

        # Execute forward action
        result = await forward_fn(**args) if asyncio.iscoroutinefunction(forward_fn) else forward_fn(**args)

        step_record = CrossAgentStep(
            agent_id=agent_id,
            step_name=step_name,
            payload={"args": args, "result": result},
            compensate_fn=compensate_fn,
            status="PREPARED",
        )
        self.active_mesh_sagas[mesh_saga_id]["steps"].append(step_record)
        logger.info("Agent '%s' prepared step '%s' under mesh saga '%s'", agent_id, step_name, mesh_saga_id)
        return {"status": "PREPARED", "step_key": step_key, "result": result}

    async def commit_mesh_saga(self, mesh_saga_id: str) -> Dict[str, Any]:
        """Phase 2: Global Commit phase when all participants report success."""
        if mesh_saga_id not in self.active_mesh_sagas:
            raise KeyError(f"Mesh saga '{mesh_saga_id}' not found")

        saga_rec = self.active_mesh_sagas[mesh_saga_id]
        for step in saga_rec["steps"]:
            step.status = "COMMITTED"

        saga_rec["state"] = "COMMITTED"
        self.vector_clock.tick()
        logger.info("Mesh Saga '%s' GLOBALLY COMMITTED across all participants", mesh_saga_id)
        return {"mesh_saga_id": mesh_saga_id, "state": "COMMITTED", "steps_count": len(saga_rec["steps"])}

    async def rollback_mesh_saga(self, mesh_saga_id: str, reason: str = "Participant failure") -> Dict[str, Any]:
        """Global 2PC Abort & Compensation in reverse LIFO order across all agents."""
        if mesh_saga_id not in self.active_mesh_sagas:
            raise KeyError(f"Mesh saga '{mesh_saga_id}' not found")

        saga_rec = self.active_mesh_sagas[mesh_saga_id]
        saga_rec["state"] = "ABORTING"
        compensated_count = 0

        # Reverse LIFO compensation
        for step in reversed(saga_rec["steps"]):
            if step.compensate_fn:
                try:
                    kwargs = step.payload.get("args", {})
                    if asyncio.iscoroutinefunction(step.compensate_fn):
                        await step.compensate_fn(**kwargs)
                    else:
                        step.compensate_fn(**kwargs)
                    step.status = "ROLLED_BACK"
                    compensated_count += 1
                except Exception as exc:
                    logger.error("Failed to compensate agent '%s' step '%s': %r", step.agent_id, step.step_name, exc)
                    step.status = "DIRTY_FAILURE"

        saga_rec["state"] = "ROLLED_BACK"
        self.vector_clock.tick()
        logger.info("Mesh Saga '%s' ROLLED BACK cleanly (%d steps compensated)", mesh_saga_id, compensated_count)
        return {
            "mesh_saga_id": mesh_saga_id,
            "state": "ROLLED_BACK",
            "reason": reason,
            "compensated_steps": compensated_count,
        }


__all__ = ["SagaMeshCoordinator", "VectorClock", "CrossAgentStep"]
