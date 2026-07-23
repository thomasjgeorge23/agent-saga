"""SelfHealingLoop multi-turn correction (#36): reformulate on refusal, back off,
escalate after exhaustion."""

from conftest import aio

from agent_saga.feedback import SelfHealingLoop, HealingOutcome
from agent_saga.gate import Decision, Verdict


async def _noop_sleep(_):
    pass


@aio
async def test_heals_after_reformulation():
    attempts = {"n": 0}

    def agent(context):
        attempts["n"] += 1
        return {"attempt": attempts["n"], "context_len": len(context)}

    def evaluate(proposal):
        if proposal["attempt"] >= 3:
            return Decision(Verdict.ALLOW, "ok", "good")
        return Decision(Verdict.BLOCK, "cap", "amount exceeds bound")

    loop = SelfHealingLoop(max_retries=3, sleep=_noop_sleep)
    out = await loop.run(agent, evaluate, tool="stripe.charge", context="charge 5000")
    assert out.healed and out.attempts == 3
    assert len(out.feedbacks) == 2                 # two refusals reformulated
    assert out.result["context_len"] > len("charge 5000")   # context grew


@aio
async def test_escalates_after_exhausting_retries():
    escalated = {}

    async def on_escalate(outcome):
        escalated["o"] = outcome

    loop = SelfHealingLoop(max_retries=3, on_escalate=on_escalate, sleep=_noop_sleep)
    out = await loop.run(lambda c: {"x": 1},
                         lambda p: Decision(Verdict.BLOCK, "r", "always refused"),
                         tool="wire.transfer")
    assert out.escalated and not out.healed and out.attempts == 3
    assert "o" in escalated and len(out.feedbacks) == 3


@aio
async def test_exponential_backoff_between_attempts():
    delays = []

    async def rec_sleep(d):
        delays.append(d)

    loop = SelfHealingLoop(max_retries=3, base_delay=0.5, backoff_factor=2.0, sleep=rec_sleep)
    await loop.run(lambda c: {}, lambda p: Decision(Verdict.BLOCK, "r", "no"), tool="t")
    # backoff between attempts 1->2 and 2->3 (none after the last)
    assert delays == [0.5, 1.0]


@aio
async def test_hallucination_score_triggers_reformulation():
    calls = {"n": 0}

    def agent(c):
        calls["n"] += 1
        return {"n": calls["n"]}

    def evaluate(p):
        score = 0.9 if p["n"] == 1 else 0.1        # first is hallucinated, then clean
        return (Decision(Verdict.ALLOW, "ok", "allowed"), score)

    out = await SelfHealingLoop(max_retries=3, sleep=_noop_sleep).run(agent, evaluate, tool="t")
    assert out.healed and out.attempts == 2


def test_max_retries_must_be_positive():
    import pytest
    with pytest.raises(ValueError):
        SelfHealingLoop(max_retries=0)
