"""Built-in embedding risk scorer (#37): score a tool call by similarity to a
corpus of known-bad actions, no model training required."""

from conftest import aio

from agent_saga.gate import (
    EmbeddingRiskScorer, GateContext, PreFlightGate, Verdict, DEFAULT_KNOWN_BAD_ACTIONS)
from agent_saga.semantics import ActionSemantics


def _ctx(tool, kwargs, sem=ActionSemantics.COMPENSABLE):
    return GateContext(tool=tool, semantics=sem, kwargs=kwargs)


def test_scorer_separates_risky_from_benign():
    s = EmbeddingRiskScorer()
    risky = s(_ctx("wire_transfer", {"amount": 5_000_000, "destination": "unknown external account"}))
    delete = s(_ctx("delete_all_records", {"table": "production", "backup": False}))
    benign = s(_ctx("get_user", {"user_id": 42}))
    assert 0.0 <= benign < risky <= 1.0
    assert benign < delete
    # a realistic threshold cleanly separates them
    assert risky > 0.5 and delete > 0.5 and benign < 0.5


def test_scorer_is_deterministic_and_zero_dependency():
    # No install, no network: two scorers agree exactly.
    a = EmbeddingRiskScorer()(_ctx("wire_transfer", {"to": "unknown"}))
    b = EmbeddingRiskScorer()(_ctx("wire_transfer", {"to": "unknown"}))
    assert a == b


def test_add_known_bad_raises_score_for_matching_action():
    s = EmbeddingRiskScorer(known_bad=["read the weather forecast"])  # unrelated corpus
    before = s(_ctx("stripe.charge", {"amount": 10000, "customer": "acme"}))
    s.add_known_bad("charge customer credit card ten thousand dollars")
    after = s(_ctx("stripe.charge", {"amount": 10000, "customer": "acme"}))
    assert after > before


def test_custom_embed_fn_is_used():
    calls = []
    def fake_embed(text):
        calls.append(text)
        return [1.0, 0.0]                      # constant vector
    s = EmbeddingRiskScorer(known_bad=["bad"], embed_fn=fake_embed)
    score = s(_ctx("t", {"k": "v"}))
    assert calls and score == 1.0              # identical constant vectors -> cosine 1


@aio
async def test_gate_requires_approval_on_high_risk():
    gate = PreFlightGate(risk_scorer=EmbeddingRiskScorer(), risk_threshold=0.5)
    risky = await gate.evaluate(_ctx("wire_transfer",
        {"amount": 5_000_000, "destination": "unknown external account"}))
    assert risky.verdict is Verdict.REQUIRE_APPROVAL
    assert "Risk Score" in risky.reason
    benign = await gate.evaluate(_ctx("get_user", {"user_id": 42}, ActionSemantics.REVERSIBLE))
    assert benign.verdict is Verdict.ALLOW


def test_default_corpus_present():
    assert len(DEFAULT_KNOWN_BAD_ACTIONS) >= 5
