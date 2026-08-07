"""`tests/test_mesh_and_deterministic.py` -- Unit tests for Distributed Mesh Saga & Deterministic Compensator.
"""

import asyncio
import pytest
import agent_saga
from agent_saga.mesh import DistributedMeshNode, DistributedMeshSaga
from agent_saga.deterministic import (
    DeterministicCompensationGuard,
    DeterministicRollbackProof,
    deterministic_compensator,
)


def test_distributed_mesh_saga():
    async def _test():
        mesh_saga = DistributedMeshSaga("saga-mesh-101", nodes=["node-1", "node-2"])
        assert len(mesh_saga.nodes) == 2

        state = {"count": 0}

        def act():
            state["count"] += 10
            return "ok"

        def comp():
            state["count"] -= 10
            return "reverted"

        res = await mesh_saga.execute_step("step-1", act, comp)
        assert res == "ok"
        assert state["count"] == 10

        results = await mesh_saga.rollback()
        assert results == [True]
        assert state["count"] == 0
        assert mesh_saga.state == "ROLLED_BACK"

    asyncio.run(_test())


def test_deterministic_compensator():
    @deterministic_compensator
    def safe_rollback(item_id: str):
        return f"reverted-{item_id}"

    assert safe_rollback.__saga_deterministic__ is True
    res = safe_rollback("item-505")
    assert res == "reverted-item-505"
    assert DeterministicCompensationGuard.verify(safe_rollback) is True

    proof = DeterministicRollbackProof("safe_rollback", verified=True)
    summary = proof.summary()
    assert summary["hallucination_risk"] == "ZERO"
    assert summary["execution_guarantee"] == "MACHINE_CHECKED"
