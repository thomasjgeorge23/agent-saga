"""AgentLoop: the goal is the transaction.

The claims under test:
1. A completed goal returns COMPLETED with its steps; side effects stand.
2. Malformed model output gets exactly one bounded repair round (the router's
   machinery), then the corrected action executes.
3. A failing tool aborts the saga and the already-committed steps are
   compensated -- an abandoned goal leaves no loose side effects.
4. Thrashing (identical call repeated) stalls the loop, which aborts and
   unwinds, rather than burning budget forever.
5. Spending the step budget without finishing raises LoopExhausted -- the loop
   never fakes an answer.
6. Every AGENT_DECISION in the WAL carries the content hash of the exact
   packed context that produced it, so "why did it do that?" is a lookup.
7. Small-model quirks (JSON-stringified nested args) are normalized before the
   tool sees them.
"""

import json

import pytest
from conftest import aio

from agent_saga import ActionSemantics, AsyncWAL, Compensation, SagaAborted, saga_scope
from agent_saga.agent_loop import AgentLoop, LoopExhausted, LoopStalled, LoopTool
from agent_saga.context_broker import ContextBroker
from agent_saga.ir import Capabilities, ChatRequest, ChatResponse
from agent_saga.router import Router

CAPS = Capabilities(context_tokens=100_000)


class ScriptedHost:
    """Plays back canned replies -- a stand-in for any model, sized like none."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    @property
    def name(self):
        return "scripted"

    def capabilities(self):
        return CAPS

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.calls.append(request)
        return ChatResponse(text=self.replies.pop(0), provider="scripted",
                            model="scripted-1")


def tool_action(name, **args):
    return json.dumps({"action": "tool", "tool": name, "args": args})


def final(answer):
    return json.dumps({"action": "final", "answer": answer})


def make_tools(undo_log, received=None):
    """reserve: commits and can be released. explode: always fails."""

    def reserve(item):
        if received is not None:
            received.append(item)
        return {"reservation": item}

    def release(item):
        undo_log.append(item)

    def explode(**kw):
        raise RuntimeError("dependency down")

    return [
        LoopTool(name="reserve", description="reserve an item",
                 parameters={"item": "string"}, fn=reserve,
                 semantics=ActionSemantics.COMPENSABLE,
                 compensate=lambda r: Compensation(
                     fn=release, kwargs={"item": r["reservation"]},
                     description="release the reservation")),
        LoopTool(name="explode", description="a tool that fails",
                 fn=explode, semantics=ActionSemantics.COMPENSABLE),
    ]


async def run_loop(tmp_path, replies, tools, **loop_kw):
    host = ScriptedHost(replies)
    wal = AsyncWAL(tmp_path / "wal.jsonl")
    await wal.start()
    try:
        broker = ContextBroker(prefix="You are a test agent.", wal=wal)
        loop = AgentLoop(Router([host]), broker, tools, **loop_kw)
        async with saga_scope(wal=wal) as ctx:
            result = await loop.run("do the thing", ctx)
        return result, await wal.read_all(), host
    finally:
        await wal.close()


# -- 1. completion ----------------------------------------------------------------

@aio
async def test_a_completed_goal_returns_its_answer_and_steps(tmp_path):
    undo, received = [], []
    result, records, _ = await run_loop(
        tmp_path,
        [tool_action("reserve", item="gpu-1"), final("reserved gpu-1")],
        make_tools(undo, received))

    assert result.status == "COMPLETED"
    assert result.answer == "reserved gpu-1"
    assert [s.tool for s in result.steps] == ["reserve"]
    assert received == ["gpu-1"]
    assert undo == []                                   # success: nothing undone
    events = [r["event"] for r in records]
    assert "SAGA_COMPLETE" in events


# -- 2. bounded repair ---------------------------------------------------------------

@aio
async def test_malformed_output_is_repaired_once_then_executes(tmp_path):
    undo, received = [], []
    result, _, host = await run_loop(
        tmp_path,
        ["Sure! I think I should reserve gpu-1 for you.",    # not the protocol
         tool_action("reserve", item="gpu-1"),               # repaired reply
         final("done")],
        make_tools(undo, received))

    assert result.status == "COMPLETED"
    assert received == ["gpu-1"]
    assert len(host.calls) == 3                         # step1 took 2 (bad+repair)
    repair = host.calls[1].messages[-1].content
    assert "failed validation" in repair


# -- 3. tool failure unwinds the attempt ------------------------------------------------

@aio
async def test_a_failing_tool_aborts_and_compensates_committed_steps(tmp_path):
    undo, received = [], []
    with pytest.raises(SagaAborted):
        await run_loop(
            tmp_path,
            [tool_action("reserve", item="gpu-1"), tool_action("explode")],
            make_tools(undo, received))

    assert received == ["gpu-1"]                        # the step really ran
    assert undo == ["gpu-1"]                            # and was really undone


# -- 4. thrashing stalls, and stalling unwinds ---------------------------------------------

@aio
async def test_identical_calls_stall_the_loop_and_unwind(tmp_path):
    undo, received = [], []
    same = tool_action("reserve", item="gpu-1")
    with pytest.raises(SagaAborted) as excinfo:
        await run_loop(tmp_path, [same, same, same],
                       make_tools(undo, received), max_repeated_calls=3)

    assert isinstance(excinfo.value.__cause__, LoopStalled)
    assert received == ["gpu-1", "gpu-1"]               # third intent never ran
    assert undo == ["gpu-1", "gpu-1"]                   # both committed calls undone


# -- 5. exhaustion never fakes an answer -----------------------------------------------------

@aio
async def test_spending_the_step_budget_raises_and_unwinds(tmp_path):
    undo, received = [], []
    with pytest.raises(SagaAborted) as excinfo:
        await run_loop(
            tmp_path,
            [tool_action("reserve", item="a"), tool_action("reserve", item="b"),
             final("never reached")],
            make_tools(undo, received), max_steps=2)

    assert isinstance(excinfo.value.__cause__, LoopExhausted)
    assert received == ["a", "b"]
    assert sorted(undo) == ["a", "b"]


# -- 6. decisions are pinned to the context that produced them --------------------------------

@aio
async def test_every_decision_carries_the_hash_of_what_the_model_saw(tmp_path):
    undo = []
    _, records, _ = await run_loop(
        tmp_path,
        [tool_action("reserve", item="gpu-1"), final("done")],
        make_tools(undo))

    packed_hashes = [r["content_hash"] for r in records
                     if r["event"] == "CONTEXT_PACKED"]
    decisions = [r for r in records if r["event"] == "AGENT_DECISION"]
    assert len(decisions) == 2                          # one per loop step
    assert [d["context_hash"] for d in decisions] == packed_hashes
    assert decisions[0]["tool"] == "reserve"
    assert decisions[1]["action"] == "final"


# -- 7. small-model quirks are normalized before the tool sees them ----------------------------

@aio
async def test_json_stringified_args_are_unwrapped(tmp_path):
    seen = {}

    def configure(payload):
        seen["payload"] = payload
        return {"ok": True}

    tools = [LoopTool(
        name="configure", description="apply a config",
        fn=configure, semantics=ActionSemantics.COMPENSABLE,
        compensate=lambda r: Compensation(fn=lambda: None, description="noop"))]

    result, _, _ = await run_loop(
        tmp_path,
        [json.dumps({"action": "tool", "tool": "configure",
                     "args": {"payload": '{"threshold": 7}'}}),   # stringified
         final("configured")],
        tools)

    assert result.status == "COMPLETED"
    assert seen["payload"] == {"threshold": 7}          # dict, not a string
