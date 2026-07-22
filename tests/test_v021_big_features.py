"""Tests for agent-saga v0.2.1 BIG enterprise features (Items 23-30)."""

import pytest
from conftest import aio

from agent_saga import (
    SlackBlockKitApp,
    TenantContext,
    set_current_tenant,
    get_current_tenant,
    SagaCloudClient,
    validate_schema,
    SchemaContractError,
    PreFlightGate,
    GateContext,
)
from agent_saga.approvals import ApprovalRequest, get_approval_store


def test_slack_block_kit_payload():
    req = ApprovalRequest(id="req_slack", saga_id="s_slack", step_id="st1", tool="stripe.charge", rule="high_risk", reason="high amount")
    block = SlackBlockKitApp.build_approval_block(req)
    assert "blocks" in block
    assert len(block["blocks"]) >= 3

    store = get_approval_store()
    store.create(req)

    cb_payload = {"actions": [{"value": '{"req_id": "req_slack", "action": "approve"}'}]}
    res = SlackBlockKitApp.handle_interactive_callback(cb_payload, approver="risk_officer_jane")
    assert res["status"] == "resolved"
    assert res["granted"] is True


@aio
async def test_dynamic_ai_risk_policy_engine():
    def mock_ml_risk_scorer(ctx: GateContext) -> float:
        if ctx.tool == "wire_transfer":
            return 0.85  # High risk
        return 0.10

    from agent_saga.semantics import ActionSemantics
    gate = PreFlightGate(risk_scorer=mock_ml_risk_scorer, risk_threshold=0.70)
    ctx_safe = GateContext(tool="read_user", semantics=ActionSemantics.REVERSIBLE, kwargs={})
    ctx_risky = GateContext(tool="wire_transfer", semantics=ActionSemantics.COMPENSABLE, kwargs={"amount": 10000})

    dec_risky = await gate.evaluate(ctx_risky)
    assert dec_risky.verdict.name == "REQUIRE_APPROVAL"
    assert "Dynamic AI Risk Score 0.85" in dec_risky.reason


def test_tenant_context_scoping():
    tenant = TenantContext(tenant_id="acme_corp", organization_id="org_99")
    token = set_current_tenant(tenant)
    try:
        curr = get_current_tenant()
        assert curr.tenant_id == "acme_corp"
        assert curr.scope_key("wal_1") == "tenant:acme_corp:wal_1"
    finally:
        set_current_tenant(None)


@aio
async def test_saga_cloud_client():
    client = SagaCloudClient(api_key="saga_cloud_key_test")
    res = await client.push_wal_records([{"event": "SAGA_START"}])
    assert res["status"] == "accepted"


def test_typed_schema_contracts():
    class StripeResult:
        def __init__(self, charge_id: str):
            self.charge_id = charge_id

    validated = validate_schema({"charge_id": "ch_123"}, StripeResult)
    assert validated.charge_id == "ch_123"
