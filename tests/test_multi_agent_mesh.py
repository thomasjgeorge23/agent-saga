import pytest
from agent_saga.multi_agent_mesh import SagaMeshCoordinator, VectorClock
from conftest import aio


@aio
async def test_vector_clock_causality():
    v1 = VectorClock(node_id="agent_alpha")
    v2 = VectorClock(node_id="agent_beta")

    v1.tick()
    assert v1.clocks["agent_alpha"] == 1

    v2.merge(v1)
    assert v2.clocks["agent_alpha"] == 1
    assert v2.clocks["agent_beta"] == 1


@aio
async def test_multi_agent_2pc_commit_flow():
    mesh = SagaMeshCoordinator(node_id="coordinator_node")

    # 1. Prepare
    await mesh.prepare_distributed_saga("mesh_tx_100", ["agent_finance", "agent_logistics"])

    # 2. Finance Step
    def charge_wallet(amount):
        return {"charge_id": "ch_99", "amount": amount}

    res_finance = await mesh.register_participant_step(
        mesh_saga_id="mesh_tx_100",
        agent_id="agent_finance",
        step_name="charge_wallet",
        forward_fn=charge_wallet,
        args={"amount": 1500},
    )
    assert res_finance["status"] == "PREPARED"

    # 3. Commit
    commit_res = await mesh.commit_mesh_saga("mesh_tx_100")
    assert commit_res["state"] == "COMMITTED"
    assert commit_res["steps_count"] == 1


@aio
async def test_multi_agent_2pc_rollback_flow():
    mesh = SagaMeshCoordinator(node_id="coordinator_node")
    refund_called = []

    def refund_wallet(amount):
        refund_called.append(amount)

    def charge_wallet(amount):
        return {"charge_id": "ch_101"}

    await mesh.prepare_distributed_saga("mesh_tx_200", ["agent_finance", "agent_inventory"])

    await mesh.register_participant_step(
        mesh_saga_id="mesh_tx_200",
        agent_id="agent_finance",
        step_name="charge_wallet",
        forward_fn=charge_wallet,
        args={"amount": 2500},
        compensate_fn=refund_wallet,
    )

    # Trigger Rollback
    rollback_res = await mesh.rollback_mesh_saga("mesh_tx_200", reason="Inventory out of stock")
    assert rollback_res["state"] == "ROLLED_BACK"
    assert rollback_res["compensated_steps"] == 1
    assert refund_called == [2500]
