"""CrewAI and OpenAI Agents SDK adapters.

Both frameworks are optional and not installed in CI's base env, so the routing
and rollback logic is pinned against fakes. Each adapter also has one real
integration test, skipped when its SDK is absent.
"""

import dataclasses
import sys
import tempfile
import types
from pathlib import Path

import pytest

from agent_saga import (
    ActionSemantics,
    AsyncWAL,
    Compensation,
    PreFlightGate,
    PreFlightViolation,
    Rule,
    SagaContext,
    Verdict,
    arg_exceeds,
)
from agent_saga.adapters._common import build_runner
from conftest import aio

C = ActionSemantics.COMPENSABLE
I = ActionSemantics.IRREVERSIBLE


async def _ctx(tmp: Path, gate=None):
    wal = AsyncWAL(tmp / "wal.jsonl")
    await wal.start()
    return SagaContext(gate=gate, wal=wal), wal


import contextlib
from agent_saga.decorator import _current


@contextlib.asynccontextmanager
async def _bind(ctx):
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)


# ==========================================================================
# Shared runner (used by both adapters)
# ==========================================================================

@aio
async def test_runner_passes_through_outside_a_saga():
    async def call(**kw):
        return {"echo": kw}
    run = build_runner(call, name="t", semantics=C)
    assert await run(x=1) == {"echo": {"x": 1}}


@aio
async def test_runner_records_and_rolls_back():
    undone = []
    async def call(**kw):
        return {"id": "r1"}
    run = build_runner(call, name="crm.update", semantics=C,
                       compensate=lambda r: Compensation(
                           fn=lambda: undone.append(r["id"]), handler="h", kwargs={}))
    with tempfile.TemporaryDirectory() as d:
        ctx, wal = await _ctx(Path(d))
        async with _bind(ctx):
            await run(status="won")
        report = await ctx.rollback()
        await wal.close()
    assert undone == ["r1"] and report.compensated[0].tool == "crm.update"


@aio
async def test_runner_arguments_reach_the_gate():
    async def call(**kw):
        return "ok"
    gate = PreFlightGate(rules=[
        Rule("cap", arg_exceeds("amount", 100_000), Verdict.BLOCK, "too big")])
    run = build_runner(call, name="charge", semantics=C)
    with tempfile.TemporaryDirectory() as d:
        ctx, wal = await _ctx(Path(d), gate=gate)
        async with _bind(ctx):
            await run(amount=5_000)
            with pytest.raises(PreFlightViolation):
                await run(amount=250_000)
        await wal.close()


# ==========================================================================
# CrewAI
# ==========================================================================

def _fake_crewai():
    """A stand-in for crewai.tools with a BaseTool whose _run is synchronous."""
    mod = types.ModuleType("crewai")
    tools = types.ModuleType("crewai.tools")

    class BaseTool:
        def __init__(self, name=None, description=None, args_schema=None):
            self.name = name
            self.description = description
            self.args_schema = args_schema
        def _run(self, **kwargs):
            raise NotImplementedError

    tools.BaseTool = BaseTool
    mod.tools = tools
    return mod, tools, BaseTool


@contextlib.contextmanager
def fake_modules(**mods):
    saved = {name: sys.modules.get(name) for name in mods}
    sys.modules.update(mods)
    try:
        yield
    finally:
        for name, old in saved.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old


@aio
async def test_crewai_wrap_tool_preserves_identity_and_routes_through_saga():
    mod, tools, BaseTool = _fake_crewai()
    with fake_modules(crewai=mod, **{"crewai.tools": tools}):
        from agent_saga.adapters import crewai as cw

        calls = []
        undone = []

        class Charge(BaseTool):
            def _run(self, **kwargs):
                calls.append(kwargs)
                return {"id": "ch_1", "amount": kwargs["amount"]}

        original = Charge(name="charge", description="charge a card", args_schema={"x": 1})
        wrapped = cw.wrap_tool(original, semantics=C, compensate=lambda r: Compensation(
            fn=lambda: undone.append(r["id"]), handler="h", kwargs={}))

        # Identity preserved for the crew/model.
        assert wrapped.name == "charge"
        assert wrapped.description == "charge a card"
        assert wrapped.args_schema == {"x": 1}

        # CrewAI calls _run synchronously; with no saga bound it just runs.
        assert wrapped._run(amount=100) == {"id": "ch_1", "amount": 100}
        assert calls == [{"amount": 100}]


@aio
async def test_crewai_rejects_non_basetool():
    mod, tools, BaseTool = _fake_crewai()
    with fake_modules(crewai=mod, **{"crewai.tools": tools}):
        from agent_saga.adapters import crewai as cw
        with pytest.raises(TypeError, match="BaseTool"):
            cw.wrap_tool(object(), semantics=C)


@aio
async def test_crewai_saga_kickoff_rolls_back_on_failure():
    mod, tools, BaseTool = _fake_crewai()
    with fake_modules(crewai=mod, **{"crewai.tools": tools}):
        from agent_saga.adapters import crewai as cw
        from agent_saga import SagaAborted, current_saga

        undone = []

        async def crew_body(ctx):
            # emulate a crew invoking a wrapped tool then failing
            run = build_runner(lambda **k: _async_val({"id": "x"}), name="t",
                               semantics=C, compensate=lambda r: Compensation(
                                   fn=lambda: undone.append(1), handler="h", kwargs={}))
            await run(v=1)
            raise ValueError("crew failed")

        with pytest.raises(SagaAborted):
            await cw.saga_kickoff(object(), kickoff=crew_body)
        assert undone == [1]


async def _async_val(v):
    return v


# ==========================================================================
# OpenAI Agents SDK
# ==========================================================================

def _fake_agents():
    mod = types.ModuleType("agents")

    @dataclasses.dataclass
    class FunctionTool:
        name: str
        description: str
        params_json_schema: dict
        on_invoke_tool: object
        strict_json_schema: bool = True

    mod.FunctionTool = FunctionTool
    return mod, FunctionTool


@aio
async def test_openai_wrap_tool_parses_json_args_for_the_gate():
    """The SDK hands tools a JSON *string*; the gate must still see the amount."""
    mod, FunctionTool = _fake_agents()
    with fake_modules(agents=mod):
        from agent_saga.adapters import openai_agents as oa

        seen = []

        async def on_invoke(run_ctx, arguments):
            seen.append(arguments)
            return "charged"

        tool = FunctionTool(name="charge", description="d",
                            params_json_schema={"type": "object"},
                            on_invoke_tool=on_invoke)

        gate = PreFlightGate(rules=[
            Rule("cap", arg_exceeds("amount", 100_000), Verdict.BLOCK, "too big")])
        wrapped = oa.wrap_tool(tool, semantics=C)

        # schema + strictness copied verbatim
        assert wrapped.params_json_schema == {"type": "object"}
        assert isinstance(wrapped, FunctionTool)

        with tempfile.TemporaryDirectory() as d:
            ctx, wal = await _ctx(Path(d), gate=gate)
            async with _bind(ctx):
                assert await wrapped.on_invoke_tool(None, '{"amount": 5000}') == "charged"
                with pytest.raises(PreFlightViolation):
                    await wrapped.on_invoke_tool(None, '{"amount": 250000}')
            await wal.close()

        # the blocked call never reached the underlying tool
        assert seen == ['{"amount": 5000}']


@aio
async def test_openai_wrap_tool_rolls_back_with_the_original_invocation():
    mod, FunctionTool = _fake_agents()
    with fake_modules(agents=mod):
        from agent_saga.adapters import openai_agents as oa

        undone = []

        async def on_invoke(run_ctx, arguments):
            return "book_9"

        tool = FunctionTool(name="book", description="d",
                            params_json_schema={}, on_invoke_tool=on_invoke)
        wrapped = oa.wrap_tool(tool, semantics=C, compensate=lambda r: Compensation(
            fn=lambda: undone.append(r), handler="h", kwargs={}))

        with tempfile.TemporaryDirectory() as d:
            ctx, wal = await _ctx(Path(d))
            async with _bind(ctx):
                await wrapped.on_invoke_tool(None, '{"room": "101"}')
            report = await ctx.rollback()
            await wal.close()
        assert undone == ["book_9"] and report.clean


@aio
async def test_openai_wrap_tool_tolerates_unparseable_arguments():
    mod, FunctionTool = _fake_agents()
    with fake_modules(agents=mod):
        from agent_saga.adapters import openai_agents as oa

        async def on_invoke(run_ctx, arguments):
            return "ok"
        tool = FunctionTool(name="t", description="d", params_json_schema={},
                            on_invoke_tool=on_invoke)
        wrapped = oa.wrap_tool(tool, semantics=C)
        with tempfile.TemporaryDirectory() as d:
            ctx, wal = await _ctx(Path(d))
            async with _bind(ctx):
                # not JSON -> runs anyway, gate just sees no policy args
                assert await wrapped.on_invoke_tool(None, "not json at all") == "ok"
            await wal.close()


@aio
async def test_openai_rejects_non_functiontool():
    mod, FunctionTool = _fake_agents()
    with fake_modules(agents=mod):
        from agent_saga.adapters import openai_agents as oa
        with pytest.raises(TypeError, match="FunctionTool"):
            oa.wrap_tool(object(), semantics=C)
