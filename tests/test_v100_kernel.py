import asyncio
import pytest
from agent_saga.kernel import AgentSagaKernel
from agent_saga.time_travel import TimeTravelDebugger
from agent_saga.zero_knowledge import ZeroKnowledgeComplianceProver
from agent_saga.governance_council import GovernanceJury


def test_kernel_page_allocation():
    kernel = AgentSagaKernel()
    page = kernel.allocate_page("p1", {"balance": 1000})
    assert page.page_id == "p1"
    snap = kernel.snapshot_kernel_state()
    assert snap["allocated_pages"] == 1


def test_time_travel_debugger():
    debugger = TimeTravelDebugger()
    debugger.record_frame(1, 1000, "STEP_1", {"val": 10})
    debugger.record_frame(2, 2000, "STEP_2", {"val": 20})

    state1 = debugger.replay_to_step(1)
    assert state1["val"] == 10

    diff = debugger.diff_frames(1, 2)
    assert diff["val"]["before"] == 10
    assert diff["val"]["after"] == 20


def test_zero_knowledge_prover():
    prover = ZeroKnowledgeComplianceProver()
    proof = prover.prove_numeric_bound(500, 100, 1000)
    assert proof.is_valid is True
    assert proof.proof_id.startswith("zk_")

    proof_invalid = prover.prove_numeric_bound(50, 100, 1000)
    assert proof_invalid.is_valid is False


def test_governance_jury():
    jury = GovernanceJury()
    res = asyncio.run(jury.evaluate_transaction("stripe.charge", {"amount": 1000}))
    assert res["passed"] is True
    assert len(res["votes"]) == 3

    res_blocked = asyncio.run(jury.evaluate_transaction("stripe.charge", {"amount": 900000}))
    assert res_blocked["passed"] is False
