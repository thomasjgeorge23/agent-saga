"""LlamaIndex and AutoGen adapters. Routing/rollback pinned against fakes;
the shared runner is covered in test_adapters_crewai_openai."""

import contextlib
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
    SagaAborted,
    SagaContext,
    Verdict,
    arg_exceeds,
)
from agent_saga.decorator import _current
from conftest import aio

C = ActionSemantics.COMPENSABLE


async def _ctx(tmp: Path, gate=None):
    wal = AsyncWAL(tmp / "wal.jsonl")
    await wal.start()
    return SagaContext(gate=gate, wal=wal)


@contextlib.asynccontextmanager
async def _bind(ctx):
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)


@contextlib.contextmanager
def fake_modules(**mods):
    saved = {k: sys.modules.get(k) for k in mods}
    sys.modules.update(mods)
    try:
        yield
    finally:
        for k, old in saved.items():
            if old is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = old


# ==========================================================================
# LlamaIndex
# ==========================================================================

def _fake_llamaindex():
    """A stand-in for llama_index.core.tools with a FunctionTool whose call the
    adapter rebuilds via from_defaults."""
    root = types.ModuleType("llama_index")
    core = types.ModuleType("llama_index.core")
    tools = types.ModuleType("llama_index.core.tools")

    class Meta:
        def __init__(self, name, description):
            self.name, self.description = name, description

    class FunctionTool:
        def __init__(self, *, fn=None, async_fn=None, name="tool", description=""):
            self.fn, self.async_fn = fn, async_fn
            self.metadata = Meta(name, description)

        @classmethod
        def from_defaults(cls, *, fn=None, async_fn=None, name="tool", description=""):
            return cls(fn=fn, async_fn=async_fn, name=name, description=description)

    tools.FunctionTool = FunctionTool
    core.tools = tools
    root.core = core
    return {"llama_index": root, "llama_index.core": core,
            "llama_index.core.tools": tools}, FunctionTool


@aio
async def test_llamaindex_wrap_preserves_metadata_and_rolls_back():
    mods, FunctionTool = _fake_llamaindex()
    with fake_modules(**mods):
        from agent_saga.adapters import llamaindex as li

        undone = []

        async def charge(amount):
            return {"id": "ch_1", "amount": amount}

        original = FunctionTool.from_defaults(async_fn=charge, name="charge",
                                              description="charge a card")
        wrapped = li.wrap_tool(original, semantics=C, compensate=lambda r: Compensation(
            fn=lambda: undone.append(r["id"]), handler="h"))

        assert isinstance(wrapped, FunctionTool)
        assert wrapped.metadata.name == "charge"
        assert wrapped.metadata.description == "charge a card"

        with tempfile.TemporaryDirectory() as d:
            ctx = await _ctx(Path(d))
            async with _bind(ctx):
                assert (await wrapped.async_fn(amount=4200))["id"] == "ch_1"
            report = await ctx.rollback()
            await ctx.wal.close()
        assert undone == ["ch_1"] and report.clean


@aio
async def test_llamaindex_sync_tool_is_wrapped_off_the_event_loop():
    mods, FunctionTool = _fake_llamaindex()
    with fake_modules(**mods):
        from agent_saga.adapters import llamaindex as li

        def lookup(q):
            return {"q": q}

        wrapped = li.wrap_tool(FunctionTool.from_defaults(fn=lookup, name="lookup"),
                               semantics=C, compensate=lambda r: Compensation(
                                   fn=lambda: None, handler="h"))
        with tempfile.TemporaryDirectory() as d:
            ctx = await _ctx(Path(d))
            async with _bind(ctx):
                assert (await wrapped.async_fn(q="hi")) == {"q": "hi"}
            await ctx.wal.close()


@aio
async def test_llamaindex_arguments_reach_the_gate():
    mods, FunctionTool = _fake_llamaindex()
    with fake_modules(**mods):
        from agent_saga.adapters import llamaindex as li

        async def charge(amount):
            return "ok"
        gate = PreFlightGate(rules=[
            Rule("cap", arg_exceeds("amount", 100_000), Verdict.BLOCK, "too big")])
        wrapped = li.wrap_tool(FunctionTool.from_defaults(async_fn=charge, name="charge"),
                               semantics=C)
        with tempfile.TemporaryDirectory() as d:
            ctx = await _ctx(Path(d), gate=gate)
            async with _bind(ctx):
                await wrapped.async_fn(amount=5_000)
                with pytest.raises(PreFlightViolation):
                    await wrapped.async_fn(amount=250_000)
            await ctx.wal.close()


# ==========================================================================
# AutoGen -- wraps a plain callable, so no framework is needed
# ==========================================================================

@aio
async def test_autogen_wrap_preserves_name_and_doc_and_rolls_back():
    from agent_saga.adapters import autogen as ag

    undone = []

    def transfer(amount: int, to: str) -> dict:
        """Transfer funds to an account."""
        return {"id": "tx_1", "amount": amount}

    wrapped = ag.wrap_tool(transfer, semantics=C, compensate=lambda r: Compensation(
        fn=lambda: undone.append(r["id"]), handler="h"))

    # Name and docstring survive -> AutoGen builds the same schema.
    assert wrapped.__name__ == "transfer"
    assert "Transfer funds" in (wrapped.__doc__ or "")

    with tempfile.TemporaryDirectory() as d:
        ctx = await _ctx(Path(d))
        async with _bind(ctx):
            assert (await wrapped(amount=100, to="acct"))["id"] == "tx_1"
        report = await ctx.rollback()
        await ctx.wal.close()
    assert undone == ["tx_1"] and report.clean


@aio
async def test_autogen_wraps_async_and_sync_callables():
    from agent_saga.adapters import autogen as ag

    async def a_tool(x): return x + 1
    def s_tool(x): return x * 2

    wa = ag.wrap_tool(a_tool, semantics=C, compensate=lambda r: Compensation(fn=lambda: None, handler="h"))
    ws = ag.wrap_tool(s_tool, semantics=C, compensate=lambda r: Compensation(fn=lambda: None, handler="h"))

    with tempfile.TemporaryDirectory() as d:
        ctx = await _ctx(Path(d))
        async with _bind(ctx):
            assert await wa(x=1) == 2
            assert await ws(x=3) == 6
        await ctx.wal.close()


@aio
async def test_autogen_passes_through_outside_a_saga():
    from agent_saga.adapters import autogen as ag

    def tool(x): return x
    wrapped = ag.wrap_tool(tool, semantics=C, compensate=lambda r: Compensation(fn=lambda: None, handler="h"))
    assert await wrapped(x=42) == 42     # no saga bound -> untouched


@aio
async def test_autogen_saga_run_rolls_back_on_failure():
    from agent_saga.adapters import autogen as ag
    from agent_saga import current_saga

    undone = []

    async def conversation(saga):
        wrapped = ag.wrap_tool(lambda **k: {"id": 1}, name="t", semantics=C,
                               compensate=lambda r: Compensation(
                                   fn=lambda: undone.append(1), handler="h"))
        await wrapped(v=1)
        raise ValueError("agent gave up")

    with pytest.raises(SagaAborted):
        await ag.saga_run(conversation)
    assert undone == [1]
