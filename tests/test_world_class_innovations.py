import asyncio
import pytest
from unittest.mock import MagicMock

from agent_saga import (
    EntanglementMatrix,
    GroundingFact,
    HallucinationDetected,
    HealingPath,
    PredictiveSentinel,
    RealityAnchor,
    SelfCorrectingLoop,
    SelfHealingGraph,
    SpeculativeEngine,
    SagaContext,
    AsyncWAL,
)
from conftest import aio


def test_reality_anchor_and_anti_hallucination():
    anchor = RealityAnchor(
        facts=[GroundingFact(key="user_id", expected_value="usr_123")],
        validators=[lambda p: p.get("amount", 0) > 0],
    )

    # Valid grounded payload
    ok, msg = anchor.verify({"user_id": "usr_123", "amount": 100})
    assert ok

    # Hallucinated payload
    bad, msg = anchor.verify({"user_id": "usr_999", "amount": 100})
    assert not bad
    assert "contradicts ground truth" in msg


@aio
async def test_self_correcting_loop():
    anchor = RealityAnchor(facts=[GroundingFact(key="status", expected_value="active")])

    def corrector(payload, feedback):
        payload["status"] = "active"
        return payload

    loop = SelfCorrectingLoop(anchor=anchor, max_retries=3, corrector=corrector)

    # Initial hallucinated payload will be auto-corrected on attempt 1
    result = await loop.execute_grounded(lambda p: f"processed_{p['status']}", {"status": "hallucinated"})
    assert result == "processed_active"


@aio
async def test_self_healing_graph():
    graph = SelfHealingGraph()

    def fallback_fn(amount, **kwargs):
        return {"healed": True, "amount": amount}

    graph.register_path(HealingPath(
        primary_tool="stripe.charge",
        fallback_tool="paypal.charge",
        fallback_fn=fallback_fn,
    ))

    healed, result, tool_used = await graph.try_heal(
        "stripe.charge",
        {"amount": 50},
        RuntimeError("Stripe API down"),
    )

    assert healed
    assert tool_used == "paypal.charge"
    assert result["healed"]


def test_speculative_shadow_engine():
    current_state = {"account_balance": 100}

    def read_state():
        return current_state

    engine = SpeculativeEngine(state_reader=read_state)
    pre = engine.capture_pre()

    # Perform action
    current_state = {"account_balance": 50}
    post = engine.capture_post()

    comp = engine.synthesize_compensation("charge_tool", pre, post)
    assert comp is not None
    assert comp.kwargs["delta"] == {"account_balance": 100}


@aio
async def test_entanglement_matrix(tmp_path):
    wal1 = AsyncWAL(tmp_path / "wal1.jsonl")
    wal2 = AsyncWAL(tmp_path / "wal2.jsonl")
    await wal1.start()
    await wal2.start()

    ctx1 = SagaContext(wal=wal1)
    ctx2 = SagaContext(wal=wal2)

    matrix = EntanglementMatrix()
    matrix.register_agent("agent_crewai", "crewai", ctx1)
    matrix.register_agent("agent_langgraph", "langgraph", ctx2, depends_on=["agent_crewai"])

    reports = await matrix.abort_all("agent_langgraph", reason="downstream failure")
    assert "agent_crewai" in reports
    assert "agent_langgraph" in reports

    await wal1.close()
    await wal2.close()


def test_predictive_sentinel():
    sentinel = PredictiveSentinel(risk_threshold=0.5)

    # Record high latency jitter and errors
    for _ in range(10):
        sentinel.record_sample("unstable_tool", latency_ms=100.0, is_error=False)
        sentinel.record_sample("unstable_tool", latency_ms=5000.0, is_error=True)

    block, risk = sentinel.should_block_preemptively("unstable_tool")
    assert block
    assert risk >= 0.5
