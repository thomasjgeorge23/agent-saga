import asyncio
import tempfile
import pytest
from pathlib import Path

from agent_saga import (
    ActionSemantics,
    ApprovalRequest,
    DAGNode,
    DAGSaga,
    DurableSagaMemory,
    HealingPath,
    PreviewPlan,
    PreviewSaga,
    SagaContext,
    SelfHealedProposal,
    SelfHealingGraph,
    UIConfirmationArtifact,
)
from conftest import aio


@aio
async def test_feature_1_speculative_preview_saga():
    preview = PreviewSaga()

    await preview.check("check_balance", lambda: {"balance": 500})
    await preview.check("verify_inventory", lambda: {"available": True, "stock": 10})
    await preview.check("calculate_delivery_fee", lambda: {"fee": 15.0})

    plan = preview.build_plan()
    assert plan.executable is True
    assert plan.preflight_results["check_balance"]["balance"] == 500
    assert plan.preflight_results["verify_inventory"]["available"] is True
    assert plan.preflight_results["calculate_delivery_fee"]["fee"] == 15.0
    assert "EXECUTABLE" in plan.summary()


@aio
async def test_feature_2_self_healing_ai_workflow():
    def primary_seller_out_of_stock(**kwargs):
        raise RuntimeError("Seller A out of stock")

    def fallback_seller_in_stock(**kwargs):
        return {"seller_id": "seller_b", "distance_meters": 400, "status": "reserved"}

    path = HealingPath(
        primary_tool="buy_from_seller_a",
        fallback_tool="buy_from_seller_b",
        fallback_fn=fallback_seller_in_stock,
    )
    graph = SelfHealingGraph(paths=[path])

    proposal = await graph.try_heal_with_proposal(
        tool="buy_from_seller_a",
        kwargs={"item_id": "item_99"},
        error=RuntimeError("Seller A out of stock"),
    )

    assert proposal is not None
    assert proposal.healed is True
    assert proposal.primary_tool == "buy_from_seller_a"
    assert proposal.fallback_tool == "buy_from_seller_b"
    assert proposal.result["seller_id"] == "seller_b"
    assert "Automatically self-healed" in proposal.explanation


def test_feature_3_suspended_state_ui_artifact():
    req = ApprovalRequest(
        id="req_123",
        step_id="step_456",
        rule="high_value_rule",
        saga_id="saga_checkout_42",
        tool="stripe.charge",
        reason="Action requires human confirmation",
        context={"amount": 120, "currency": "USD"},
        expires_at=1000.0,
    )

    artifact = req.to_ui_artifact()
    assert isinstance(artifact, UIConfirmationArtifact)
    assert artifact.saga_id == "saga_checkout_42"
    assert artifact.tool_name == "stripe.charge"
    assert "agent-saga approvals approve" in artifact.approve_action
    assert artifact.parameters["amount"] == 120


@aio
async def test_feature_4_multi_step_goal_decomposition_dag():
    from agent_saga.wal import AsyncWAL
    wal = AsyncWAL()
    await wal.start()

    parent_ctx = SagaContext(wal=wal, saga_id="community_event_root")
    dag = DAGSaga(parent_ctx=parent_ctx, name="organize_tool_swap")

    dag.add_node(
        node_id="create_board_event",
        action_fn=lambda ctx: {"event_id": "ev_001"},
        description="Create local request board event",
    )
    dag.add_node(
        node_id="reserve_community_hall",
        action_fn=lambda ctx: {"hall_id": "hall_east"},
        dependencies=["create_board_event"],
        description="Reserve community hall location",
    )
    dag.add_node(
        node_id="broadcast_notification",
        action_fn=lambda ctx: {"notified_users": 50},
        dependencies=["reserve_community_hall"],
        description="Broadcast notification to 50 nearby active users",
    )

    results = await dag.execute_dag()
    assert len(results) == 3
    assert results["create_board_event"]["event_id"] == "ev_001"
    assert results["reserve_community_hall"]["hall_id"] == "hall_east"
    assert results["broadcast_notification"]["notified_users"] == 50

    await wal.close()


def test_feature_5_durable_saga_memory(tmp_path):
    memory = DurableSagaMemory(tmp_path / "memory_store")

    saga_id = "multi_day_order_77"
    memory.persist_state(
        saga_id=saga_id,
        status="ACTIVE",
        state_data={"step": "shipping", "tracking": "TRK_9901"},
        metadata={"item": "Power Drill", "seller": "Bob Tools"},
    )

    loaded = memory.load_state(saga_id)
    assert loaded is not None
    assert loaded["status"] == "ACTIVE"
    assert loaded["state_data"]["tracking"] == "TRK_9901"

    active_sagas = memory.list_active_sagas()
    assert len(active_sagas) == 1
    assert active_sagas[0]["saga_id"] == saga_id

    summary = memory.get_saga_status_summary(saga_id)
    assert "Power Drill" in summary
    assert "ACTIVE" in summary
