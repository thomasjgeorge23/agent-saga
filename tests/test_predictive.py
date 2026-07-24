"""Predictive intent pre-execution (Phase 1.1): run the safe part of a saga
before intent is confirmed, without ever performing an unasked-for effect."""

import pytest
from conftest import aio

from agent_saga.predictive import (
    DEFAULT_TTL, PredictiveExecutor, SpeculationRefused, intent_hash)
from agent_saga.semantics import ActionSemantics as S


def _px(clock=None, **kw):
    return PredictiveExecutor(clock=clock or (lambda: 1000.0), **kw)


# -- the safety rule ----------------------------------------------------------

@aio
async def test_refuses_to_speculate_anything_with_effects():
    px = _px()
    for sem in (S.COMPENSABLE, S.IRREVERSIBLE):
        with pytest.raises(SpeculationRefused) as exc:
            await px.speculate("stripe.charge", lambda: "CHARGED",
                               intent="pay the invoice", semantics=sem)
        assert sem.name in str(exc.value)
    assert px.stats.refused == 2


@aio
async def test_refused_speculation_never_runs_the_forward():
    px = _px()
    ran = {"n": 0}

    def charge():
        ran["n"] += 1          # must never happen
        return "CHARGED"

    with pytest.raises(SpeculationRefused):
        await px.speculate("stripe.charge", charge, intent="pay",
                           semantics=S.IRREVERSIBLE)
    assert ran["n"] == 0


# -- the latency win ----------------------------------------------------------

@aio
async def test_confirm_returns_cached_result_without_rerunning():
    px = _px()
    calls = {"n": 0}

    def lookup():
        calls["n"] += 1
        return {"in_stock": 7}

    await px.speculate("inventory.check", lookup, intent="do i have a wrench")
    assert calls["n"] == 1
    hit = px.confirm("do i have a wrench")
    assert hit is not None and hit.result == {"in_stock": 7}
    assert calls["n"] == 1                      # the win: no second run
    assert px.stats.hits == 1


@aio
async def test_intent_is_whitespace_insensitive_at_the_edges():
    px = _px()
    await px.speculate("t", lambda: 1, intent="find a plumber")
    assert px.confirm("  find a plumber  ") is not None


# -- the guards ---------------------------------------------------------------

@aio
async def test_changed_intent_is_never_served_a_stale_answer():
    px = _px()
    await px.speculate("inventory.check", lambda: {"in_stock": 7},
                       intent="do i have a wrench")
    assert px.confirm("do i have a hammer") is None       # different question
    assert px.stats.misses == 1


@aio
async def test_expired_speculation_is_not_served():
    clock = {"t": 1000.0}
    px = _px(clock=lambda: clock["t"], ttl=60)
    await px.speculate("t", lambda: "stale", intent="abandoned draft")
    clock["t"] += 61
    assert px.confirm("abandoned draft") is None
    assert px.stats.expired >= 1


@aio
async def test_a_hit_is_single_use():
    px = _px()
    await px.speculate("t", lambda: 1, intent="once")
    assert px.confirm("once") is not None
    assert px.confirm("once") is None            # consumed


@aio
async def test_forged_lease_is_rejected():
    px = _px()
    await px.speculate("t", lambda: "trusted", intent="forge me")
    px._entries[intent_hash("forge me")].lease = "de" * 32   # tamper
    assert px.confirm("forge me") is None


@aio
async def test_lease_from_another_process_is_not_honoured():
    a, b = _px(), _px()                          # independent secrets
    spec = await a.speculate("t", lambda: 1, intent="x")
    b._entries[spec.intent_hash] = spec          # inject A's speculation into B
    assert b.confirm("x") is None                # B refuses A's lease


@aio
async def test_tool_mismatch_misses():
    px = _px()
    await px.speculate("inventory.check", lambda: 1, intent="q")
    assert px.confirm("q", tool="stripe.charge") is None


# -- failure and memory -------------------------------------------------------

@aio
async def test_speculative_failure_is_swallowed_and_never_served():
    px = _px()

    def boom():
        raise RuntimeError("upstream down")

    spec = await px.speculate("t", boom, intent="q")      # must not raise
    assert spec.error is not None
    assert px.confirm("q") is None                        # falls back to real run


@aio
async def test_memory_is_bounded():
    px = _px(max_entries=5)
    for i in range(50):
        await px.speculate("t", lambda: i, intent=f"intent-{i}")
    assert px.pending <= 5


@aio
async def test_cancel_and_sweep():
    clock = {"t": 1000.0}
    px = _px(clock=lambda: clock["t"], ttl=10)
    await px.speculate("t", lambda: 1, intent="cancel me")
    assert px.cancel("cancel me") is True
    assert px.cancel("cancel me") is False

    await px.speculate("t", lambda: 1, intent="expire me")
    clock["t"] += 11
    assert px.sweep() == 1 and px.pending == 0


def test_rejects_bad_config():
    with pytest.raises(ValueError):
        PredictiveExecutor(ttl=0)
    with pytest.raises(ValueError):
        PredictiveExecutor(max_entries=0)
