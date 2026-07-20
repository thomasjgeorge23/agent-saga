"""LangGraph adapter.

The routing/rollback logic is tested against fakes with no LangChain installed,
because that is the part that matters and the part we can pin. A single
integration test exercises the real StructuredTool packaging and is skipped
when langchain_core is absent.
"""

import tempfile
from pathlib import Path

import pytest

from agent_saga import (
    ActionSemantics,
    AsyncWAL,
    Compensation,
    PreFlightGate,
    PreFlightViolation,
    Rule,
    SagaAborted,
    SagaContext,
    Verdict,
    arg_exceeds,
)
from agent_saga.adapters.langgraph import build_runner, saga_run
from agent_saga.decorator import saga_scope
from conftest import aio

C = ActionSemantics.COMPENSABLE
I = ActionSemantics.IRREVERSIBLE


class FakeTool:
    """The slice of a LangChain tool the adapter actually calls: an async
    `ainvoke(input_dict)`. Records what it received and what it returned."""

    def __init__(self, name, fn):
        self.name = name
        self._fn = fn
        self.calls = []

    async def ainvoke(self, input: dict):
        self.calls.append(dict(input))
        return self._fn(**input)


async def _ctx(tmp: Path, gate=None):
    wal = AsyncWAL(tmp / "wal.jsonl")
    await wal.start()
    return SagaContext(gate=gate, wal=wal), wal


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------

@aio
async def test_wrapped_tool_passes_through_outside_a_saga():
    tool = FakeTool("charge", lambda amount: {"id": "ch_1", "amount": amount})
    run = build_runner(tool.ainvoke, name="charge", semantics=C)
    # No saga on the contextvar -> the tool runs untouched.
    assert await run(amount=100) == {"id": "ch_1", "amount": 100}


@aio
async def test_wrapped_tool_records_on_the_active_saga_and_rolls_back():
    undone = []
    tool = FakeTool("charge", lambda amount: {"id": "ch_9", "amount": amount})
    run = build_runner(
        tool.ainvoke, name="stripe.charge", semantics=C,
        compensate=lambda r: Compensation(
            fn=lambda: undone.append(r["id"]), handler="x",
            kwargs={}, idempotency_key="k"))

    with tempfile.TemporaryDirectory() as d:
        ctx, wal = await _ctx(Path(d))
        async with _bind(ctx):
            assert (await run(amount=4200))["id"] == "ch_9"
        report = await ctx.rollback()
        await wal.close()

    assert tool.calls == [{"amount": 4200}]
    assert undone == ["ch_9"]
    assert report.compensated[0].tool == "stripe.charge"


@aio
async def test_wrapped_tool_arguments_reach_the_gate():
    """The whole point of policy_args: a threshold rule must see the amount that
    is otherwise captured in the forward closure."""
    tool = FakeTool("charge", lambda amount: {"id": "ch_1", "amount": amount})
    gate = PreFlightGate(rules=[
        Rule("cap", arg_exceeds("amount", 100_000), Verdict.BLOCK, "too big")])
    run = build_runner(tool.ainvoke, name="charge", semantics=C)

    with tempfile.TemporaryDirectory() as d:
        ctx, wal = await _ctx(Path(d), gate=gate)
        async with _bind(ctx):
            await run(amount=5_000)                     # under
            with pytest.raises(PreFlightViolation):
                await run(amount=250_000)               # over -> blocked
        await wal.close()

    assert tool.calls == [{"amount": 5_000}]            # blocked call never ran


# --------------------------------------------------------------------------
# saga_run against a fake graph
# --------------------------------------------------------------------------

class FakeGraph:
    """Drives a sequence of wrapped tools, then optionally raises -- standing in
    for a compiled LangGraph whose node hallucinates mid-run."""

    def __init__(self, steps, boom=False):
        self.steps, self.boom = steps, boom

    async def ainvoke(self, input, config=None):
        for run, kwargs in self.steps:
            await run(**kwargs)
        if self.boom:
            raise ValueError("a graph node hallucinated")
        return {"ok": True}


@aio
async def test_saga_run_commits_on_success():
    undone = []
    tool = FakeTool("crm", lambda **k: {"id": "acct_1"})
    run = build_runner(tool.ainvoke, name="crm.update", semantics=C,
                       compensate=lambda r: Compensation(
                           fn=lambda: undone.append(1), handler="x", kwargs={}))
    graph = FakeGraph([(run, {"status": "won"})])

    out = await saga_run(graph, {"msg": "go"})
    assert out == {"ok": True}
    assert undone == []                                 # success -> no rollback


@aio
async def test_saga_run_rolls_back_every_tool_on_graph_failure():
    order = []
    tools = []
    for i in range(3):
        t = FakeTool(f"t{i}", lambda **k: {"i": k})
        r = build_runner(t.ainvoke, name=f"tool{i}", semantics=C,
                         compensate=lambda r, i=i: Compensation(
                             fn=lambda i=i: order.append(f"undo{i}"),
                             handler="x", kwargs={}))
        tools.append((r, {"n": i}))
    graph = FakeGraph(tools, boom=True)

    with pytest.raises(SagaAborted) as exc:
        await saga_run(graph, {})

    assert order == ["undo2", "undo1", "undo0"]         # LIFO
    assert isinstance(exc.value.cause, ValueError)
    assert exc.value.report.clean


@aio
async def test_saga_run_can_return_the_report_instead_of_raising():
    tool = FakeTool("t", lambda **k: None)
    run = build_runner(tool.ainvoke, name="tool", semantics=C,
                       compensate=lambda r: Compensation(
                           fn=lambda: None, handler="x", kwargs={}))
    report = await saga_run(FakeGraph([(run, {})], boom=True), {}, reraise=False)
    assert report.clean and len(report.compensated) == 1


@aio
async def test_saga_run_honors_a_gate_and_blocks_before_the_tool_runs():
    tool = FakeTool("email", lambda **k: "sent")
    run = build_runner(tool.ainvoke, name="email.send", semantics=I)
    graph = FakeGraph([(run, {"to": "x@y.com"})])

    with pytest.raises(SagaAborted) as exc:
        await saga_run(graph, {})                       # default gate blocks IRREVERSIBLE

    assert isinstance(exc.value.cause, PreFlightViolation)
    assert tool.calls == []                             # nothing was sent


@aio
async def test_saga_run_invoke_override_receives_the_context():
    seen = {}

    async def custom(ctx):
        seen["saga_id"] = ctx.saga_id
        return "custom-result"

    out = await saga_run(FakeGraph([]), invoke=custom)
    assert out == "custom-result"
    assert "saga_id" in seen


# --------------------------------------------------------------------------
# Integration: real StructuredTool packaging (skipped without LangChain)
# --------------------------------------------------------------------------

@aio
async def test_wrap_tool_produces_a_real_structuredtool():
    pytest.importorskip("langchain_core")
    from langchain_core.tools import StructuredTool
    from agent_saga.adapters.langgraph import wrap_tool

    undone = []

    def book_room(room: str, nights: int) -> dict:
        """Book a hotel room for a number of nights."""
        return {"confirmation": f"{room}-{nights}"}

    wrapped = wrap_tool(
        book_room, semantics=C,
        compensate=lambda r: Compensation(
            fn=lambda: undone.append(r["confirmation"]),
            handler="hotel.cancel", kwargs={}, idempotency_key="k"))

    assert isinstance(wrapped, StructuredTool)
    assert wrapped.name == "book_room"
    assert set(wrapped.args) == {"room", "nights"}      # schema preserved

    with tempfile.TemporaryDirectory() as d:
        ctx, wal = await _ctx(Path(d))
        async with _bind(ctx):
            # LangGraph invokes a structured tool with a dict.
            assert (await wrapped.ainvoke({"room": "101", "nights": 2}))[
                "confirmation"] == "101-2"
        report = await ctx.rollback()
        await wal.close()
    assert undone == ["101-2"] and report.clean


@aio
async def test_real_langchain_tools_roll_back_through_saga_run():
    """The actual promise: real @tool tools, a graph that raises, everything
    unwinds LIFO -- exercised against langchain_core, not a fake."""
    pytest.importorskip("langchain_core")
    from langchain_core.tools import tool as lc_tool
    from agent_saga.adapters.langgraph import wrap_tool

    world = {"crm": "prospect", "ledger": []}

    @lc_tool
    def update_crm(status: str) -> dict:
        """Set the CRM status."""
        prev = world["crm"]
        world["crm"] = status
        return {"prev": prev}

    @lc_tool
    def charge(amount: int) -> dict:
        """Charge the customer amount in cents."""
        world["ledger"].append(amount)
        return {"id": "ch_1", "amount": amount}

    saga_crm = wrap_tool(update_crm, semantics=C, compensate=lambda r: Compensation(
        fn=lambda: world.__setitem__("crm", r["prev"]), handler="h", kwargs={}))
    saga_charge = wrap_tool(charge, semantics=C, compensate=lambda r: Compensation(
        fn=lambda: world["ledger"].append(-r["amount"]), handler="h", kwargs={}))

    class Graph:
        async def ainvoke(self, inp, config=None):
            await saga_crm.ainvoke({"status": "customer"})
            await saga_charge.ainvoke({"amount": 49900})
            raise ValueError("hallucinated field")

    with pytest.raises(SagaAborted) as exc:
        await saga_run(Graph(), {})

    assert exc.value.report.clean
    assert world["crm"] == "prospect"          # restored
    assert sum(world["ledger"]) == 0           # charge + refund net to zero


@aio
async def test_wrap_tool_gives_a_clear_error_for_a_function_without_a_docstring():
    pytest.importorskip("langchain_core")
    from agent_saga.adapters.langgraph import wrap_tool

    def no_doc(x: int) -> int:
        return x

    with pytest.raises(ValueError, match="docstring"):
        wrap_tool(no_doc, semantics=C)


# --------------------------------------------------------------------------

import contextlib
from agent_saga.decorator import _current


@contextlib.asynccontextmanager
async def _bind(ctx):
    """Put a SagaContext on the contextvar without opening a full scope, so a
    test can drive execute() and then assert on rollback() itself."""
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)
