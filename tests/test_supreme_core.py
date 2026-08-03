import asyncio
import pytest
from agent_saga.supreme_core import QuantumResilientMerkleWAL, AutonomousHealingOrchestrator
from agent_saga.compiler_v2 import SupremeFullStackCompiler
from agent_saga.agentic_mesh import SagaSwarmMesh
from agent_saga.semantics import ActionSemantics, SagaStep


def test_quantum_resilient_wal():
    wal = QuantumResilientMerkleWAL()
    token1 = wal.append_record("STEP_COMMIT", {"tool": "stripe.charge", "amount": 100})
    token2 = wal.append_record("STEP_COMMIT", {"tool": "wallet.debit", "amount": 100})

    assert token1.startswith("QWAL:")
    assert token2.startswith("QWAL:")
    assert len(wal.records) == 2

    root = wal.compute_merkle_root()
    assert len(root) == 128  # SHA3-512 hex length


def test_autonomous_healing_orchestrator():
    orchestrator = AutonomousHealingOrchestrator()
    healed = []

    def mock_inverse():
        healed.append("healed")

    orchestrator.register_inverse("failing_tool", mock_inverse)

    step = SagaStep(
        tool="failing_tool",
        semantics=ActionSemantics.COMPENSABLE,
    )

    success = asyncio.run(orchestrator.auto_heal_failed_step(step, ValueError("Test failure")))
    assert success is True
    assert healed == ["healed"]


def test_fullstack_compiler():
    def sample_workflow(amount: float):
        return "ok"

    compiler = SupremeFullStackCompiler(project_name="test_app")
    compiler.add_workflow(sample_workflow)
    code = compiler.generate_nextjs_page_component()
    assert "GeneratedSagaPage" in code


def test_swarm_mesh():
    mesh = SagaSwarmMesh(cluster_id="test_cluster")
    mesh.add_node("node_1")
    mesh.add_node("node_2")

    acks = asyncio.run(mesh.broadcast_wal_entry({"type": "WAL_COMMIT", "seq": 1}))
    assert acks == 2
    assert mesh.leader_id == "node_1"
