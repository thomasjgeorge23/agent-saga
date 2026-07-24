import asyncio
import tempfile
import pytest

from agent_saga import (
    ActionSemantics,
    Compensation,
    GateContext,
    HardwareApprovalError,
    MCPTransactionProxy,
    MerkleMeshSync,
    MultiSigApprovalProvider,
    PreFlightGate,
    SagaContext,
    Verdict,
    saga_scope,
)
from conftest import aio


@aio
async def test_adaptive_compensation_retries():
    attempts = 0

    async def failing_compensation():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionError("Transient network failure")
        return {"status": "recovered"}

    from agent_saga.wal import AsyncWAL
    wal = AsyncWAL()
    await wal.start()

    ctx = SagaContext(wal=wal, compensation_max_retries=3, compensation_retry_delay=0.01)
    await ctx.begin()

    step = await ctx.execute(
        tool="payment.charge",
        semantics=ActionSemantics.COMPENSABLE,
        forward=lambda: {"tx_id": "tx_123"},
        compensate=lambda res: Compensation(fn=failing_compensation),
    )

    report = await ctx.rollback()
    assert report.clean
    assert len(report.compensated) == 1
    assert attempts == 3
    await ctx.finish(aborted=True, clean=True)
    await wal.close()


@aio
async def test_nested_child_saga_scopes():
    from agent_saga.wal import AsyncWAL
    wal = AsyncWAL()
    await wal.start()

    parent_ctx = SagaContext(wal=wal, saga_id="parent_root", name="parent_saga")
    await parent_ctx.begin()

    child_ctx = parent_ctx.create_child_scope("payment_subtree")
    assert child_ctx.saga_id == "parent_root.payment_subtree"
    assert "payment_subtree" in child_ctx.name

    await parent_ctx.finish()
    await wal.close()


@aio
async def test_preflight_gate_schema_validation():
    gate = PreFlightGate()
    gate.add_schema_rule("transfer_funds", ["sender_id", "receiver_id", "amount"])

    # Valid call
    valid_ctx = GateContext(
        tool="transfer_funds",
        semantics=ActionSemantics.COMPENSABLE,
        kwargs={"sender_id": "u1", "receiver_id": "u2", "amount": 100},
    )
    decision1 = await gate.evaluate(valid_ctx)
    assert decision1.verdict != Verdict.BLOCK

    # Invalid call (missing amount)
    invalid_ctx = GateContext(
        tool="transfer_funds",
        semantics=ActionSemantics.COMPENSABLE,
        kwargs={"sender_id": "u1", "receiver_id": "u2"},
    )
    decision2 = await gate.evaluate(invalid_ctx)
    assert decision2.verdict == Verdict.BLOCK
    assert "schema_validation" in decision2.rule


def test_multisig_quorum_approvals():
    from cryptography.hazmat.primitives.asymmetric import ed25519

    k1 = ed25519.Ed25519PrivateKey.generate()
    k2 = ed25519.Ed25519PrivateKey.generate()
    pub1 = k1.public_key().public_bytes_raw()
    pub2 = k2.public_key().public_bytes_raw()

    provider = MultiSigApprovalProvider(required_signatures=2, credentials={"c1": pub1, "c2": pub2})

    ctx = GateContext(tool="wire_transfer", semantics=ActionSemantics.IRREVERSIBLE, kwargs={"amount": 50000})
    ch = provider.challenge_for(ctx)

    sig1 = k1.sign(ch.signing_payload())
    sig2 = k2.sign(ch.signing_payload())

    # First signature recorded, quorum not satisfied yet
    res1 = provider.submit(ch.challenge_id, "c1", sig1)
    assert not res1
    assert not provider.approved(ctx)

    # Re-issue challenge for second sign off
    ch2 = provider.challenge_for(ctx)
    sig2_new = k2.sign(ch2.signing_payload())

    res2 = provider.submit(ch2.challenge_id, "c2", sig2_new)
    assert res2
    assert provider.approved(ctx)


def test_merkle_mesh_sync():
    r1 = {"ts": 1.0, "seq": 1, "saga_id": "s1", "event": "E1"}
    r2 = {"ts": 2.0, "seq": 2, "saga_id": "s1", "event": "E2"}

    root1 = MerkleMeshSync.compute_root([r1, r2])
    root2 = MerkleMeshSync.compute_root([r1, r2])
    assert root1 == root2

    merged, reconciled = MerkleMeshSync.reconcile_peers([r1], [r1, r2])
    assert reconciled is True
    assert len(merged) == 2


@aio
async def test_mcp_transaction_proxy():
    proxy = MCPTransactionProxy()

    async def mcp_refund_tool(charge_id: str):
        return {"status": "refunded", "charge_id": charge_id}

    proxy.wrap_mcp_tool(
        tool_name="stripe_refund",
        tool_fn=mcp_refund_tool,
        semantics=ActionSemantics.REVERSIBLE,
    )

    result = await proxy.execute_mcp_request("stripe_refund", {"charge_id": "ch_99"})
    assert result["status"] == "refunded"
