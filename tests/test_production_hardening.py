"""The hardening pass: what separates the demo from the product.

The claims under test:
1. Broker verification is O(referenced documents) per pack, not O(spans x
   document) -- one fetch and one hash per doc, with per-slice work only when
   a document actually changed. And the fast path still catches tampering.
2. A bounded WARM tier displaces oldest-first and records what it forgot.
3. A hung host is a named, fallback-able rejection within the attempt
   deadline -- never a frozen agent.
4. Transient host failures are retried on the SAME host (bounded, with
   backoff) before falling back; latency is accounted per host.
5. The loop enforces wall-clock and token budgets: exceeding either raises
   and therefore unwinds the attempt's side effects.
6. Constructing a loop does not mutate the caller's broker.
"""

import asyncio
import json

import pytest
from conftest import aio

from agent_saga import ActionSemantics, AsyncWAL, Compensation, SagaAborted, saga_scope
from agent_saga.agent_loop import (
    AgentLoop,
    LoopBudgetExceeded,
    LoopDeadlineExceeded,
    LoopTool,
)
from agent_saga.context_broker import ContextBroker
from agent_saga.ir import Capabilities, ChatRequest, ChatResponse, user
from agent_saga.router import HostTimeout, Router, RoutingPolicy

CAPS = Capabilities(context_tokens=100_000)
DOC = "line one\nline two\nline three\n" * 40


# -- 1. verification cost and correctness ------------------------------------------

class CountingCold:
    """MemoryColdStore that counts get() calls -- the fast-path meter."""

    def __init__(self):
        self._docs, self.gets = {}, 0

    def get(self, doc_id):
        self.gets += 1
        return self._docs.get(doc_id)

    def put(self, doc_id, text):
        self._docs[doc_id] = text


def test_pack_fetches_each_document_once_no_matter_how_many_spans():
    cold = CountingCold()
    broker = ContextBroker(prefix="P", cold=cold)
    spans = broker.add_document("doc", DOC, chunk_chars=50)   # ~24 spans
    for i in range(0, len(spans), 2):
        broker.admit_summary(f"summary of chunk {i}", [spans[i]])

    cold.gets = 0
    broker.pack(5000)
    assert cold.gets == 1          # one fetch, one hash -- not one per span


def test_the_fast_path_still_catches_tampering():
    cold = CountingCold()
    broker = ContextBroker(prefix="P", cold=cold)
    spans = broker.add_document("doc", DOC)
    sid = broker.admit_summary("summary", spans)

    cold.put("doc", DOC.replace("two", "TWO"))               # same length, new bytes
    packed = broker.pack(5000)
    assert sid in packed.evicted
    assert "has changed" in packed.evicted[sid]


def test_tail_growth_survives_the_changed_doc_slow_path():
    """Append-only growth changes the doc hash but not the receipted slices;
    the per-slice fallback keeps those summaries alive."""
    broker = ContextBroker(prefix="P")
    spans = broker.add_document("log", DOC)
    sid = broker.admit_summary("summary of the head", spans)

    broker.cold.put("log", DOC + "line appended later\n")
    packed = broker.pack(5000)
    assert sid in packed.included


# -- 2. bounded WARM -----------------------------------------------------------------

def test_warm_capacity_displaces_oldest_and_records_it():
    broker = ContextBroker(prefix="P", max_warm_entries=2)
    spans = broker.add_document("doc", DOC)
    first = broker.admit_summary("one", spans)
    second = broker.admit_summary("two", spans)
    third = broker.admit_summary("three", spans)

    packed = broker.pack(5000)
    assert first not in packed.included
    assert second in packed.included and third in packed.included
    assert "WARM capacity of 2" in broker.displaced[first]


# -- 3 & 4. hung and flaky hosts --------------------------------------------------------

class HungHost:
    @property
    def name(self):
        return "hung"

    def capabilities(self):
        return CAPS

    async def complete(self, request):
        await asyncio.sleep(30)
        raise AssertionError("unreachable")


class FlakyOnceHost:
    def __init__(self):
        self.calls = 0

    @property
    def name(self):
        return "flaky-once"

    def capabilities(self):
        return CAPS

    async def complete(self, request):
        self.calls += 1
        if self.calls == 1:
            raise ConnectionResetError("transient blip")
        return ChatResponse(text="ok", provider=self.name, model="m")


class SteadyHost:
    def __init__(self):
        self.calls = 0

    @property
    def name(self):
        return "steady"

    def capabilities(self):
        return CAPS

    async def complete(self, request):
        self.calls += 1
        return ChatResponse(text="ok", provider=self.name, model="m")


@aio
async def test_a_hung_host_times_out_and_the_fallback_serves():
    steady = SteadyHost()
    router = Router([HungHost(), steady],
                    RoutingPolicy(prefer=("hung", "steady")),
                    attempt_timeout=0.05, backoff_base=0)

    response, decision = await router.complete(ChatRequest(messages=(user("hi"),)))
    assert response.provider == "steady"
    assert any("no response within 0.05s" in r for r in decision.rejections["hung"])
    # The wait is on the record. Not asserted against 0.05 itself: Windows'
    # monotonic clock ticks at ~15.6ms on Python < 3.13, so a 50ms wait can
    # legitimately measure 47ms.
    assert decision.timings["hung"] > 0


@aio
async def test_transient_failures_retry_on_the_same_host_before_falling_back():
    flaky, steady = FlakyOnceHost(), SteadyHost()
    router = Router([flaky, steady],
                    RoutingPolicy(prefer=("flaky-once", "steady")),
                    attempts_per_host=2, backoff_base=0)

    response, decision = await router.complete(ChatRequest(messages=(user("hi"),)))
    assert response.provider == "flaky-once"         # recovered in place
    assert flaky.calls == 2
    assert steady.calls == 0                         # fallback never needed
    assert decision.rejections.get("flaky-once") is None


# -- 5. loop budgets unwind, never overspend ----------------------------------------------

class ScriptedHost:
    def __init__(self, replies):
        self.replies = list(replies)

    @property
    def name(self):
        return "scripted"

    def capabilities(self):
        return CAPS

    async def complete(self, request):
        return ChatResponse(text=self.replies.pop(0), provider="scripted", model="m")


def _tools(undo):
    def reserve(item):
        return {"reservation": item}

    return [LoopTool(
        name="reserve", description="reserve", fn=reserve,
        semantics=ActionSemantics.COMPENSABLE,
        compensate=lambda r: Compensation(
            fn=lambda item: undo.append(item), kwargs={"item": r["reservation"]},
            description="release"))]


def _action(item):
    return json.dumps({"action": "tool", "tool": "reserve", "args": {"item": item}})


async def _run(tmp_path, replies, undo, **loop_kw):
    wal = AsyncWAL(tmp_path / "wal.jsonl")
    await wal.start()
    try:
        broker = ContextBroker(prefix="test", wal=wal)
        loop = AgentLoop(Router([ScriptedHost(replies)]), broker,
                         _tools(undo), **loop_kw)
        async with saga_scope(wal=wal) as ctx:
            return await loop.run("goal", ctx)
    finally:
        await wal.close()


@aio
async def test_the_wall_clock_deadline_unwinds_the_attempt(tmp_path):
    undo = []
    with pytest.raises(SagaAborted) as excinfo:
        await _run(tmp_path, [_action("a")], undo, deadline_s=0.0)
    assert isinstance(excinfo.value.__cause__, LoopDeadlineExceeded)
    assert undo == []                                # nothing ran, nothing to undo


@aio
async def test_the_token_budget_unwinds_committed_work(tmp_path):
    undo = []
    with pytest.raises(SagaAborted) as excinfo:
        await _run(tmp_path, [_action("a"), _action("b")], undo,
                   max_total_tokens=1)
    assert isinstance(excinfo.value.__cause__, LoopBudgetExceeded)
    assert undo == ["a"]                             # step 1 ran, and was undone


# -- 6. the loop borrows the broker, it does not own it ---------------------------------

def test_constructing_a_loop_does_not_mutate_the_broker():
    broker = ContextBroker(prefix="the caller's prefix")
    AgentLoop(Router([SteadyHost()]), broker, _tools([]))
    AgentLoop(Router([SteadyHost()]), broker, _tools([]))    # twice, deliberately
    assert broker.prefix == "the caller's prefix"
