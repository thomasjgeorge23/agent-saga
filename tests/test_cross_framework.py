"""One saga across frameworks that have never heard of each other.

The claim: a boundary opened once is joined by any adapter's tool, whichever
framework calls it, because `build_runner` reads the `current_saga()`
contextvar rather than any framework's state. A failure anywhere unwinds
everything, LIFO, in one transaction.

This is tested against the real routing core and a real `langchain_core` tool
through the real `adapters.langgraph.wrap_tool`. Nothing here asserts on the
example's prose -- it asserts on the WAL and the world.
"""

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import aio

from agent_saga import ActionSemantics, AsyncWAL, Compensation, saga_scope
from agent_saga.adapters._common import build_runner
from agent_saga.context import SagaAborted

C = ActionSemantics.COMPENSABLE
ROOT = Path(__file__).resolve().parent.parent


def make_tool(name, world, key, *, fail=False):
    """A tool in the shape every adapter reduces to: an async call routed
    through build_runner. This is literally what crewai/autogen/llamaindex
    wrap_tool build, minus the framework's own base class."""

    async def call(**kwargs):
        if fail:
            raise ConnectionError(f"{name} unreachable")
        entry = f"{key}_{len(world[key]) + 1}"
        world[key].append(entry)
        return {"id": entry}

    def undo(entry_id):
        world[key] = [e for e in world[key] if e != entry_id]

    return build_runner(
        call, name=name, semantics=C,
        compensate=lambda r: Compensation(
            fn=undo, kwargs={"entry_id": r["id"]}, description=f"undo {name}")
        if r else None)


# -- the core claim ------------------------------------------------------------

@aio
async def test_one_boundary_spans_tools_from_different_frameworks(tmp_path):
    world = {"crew": [], "autogen": []}
    crew_tool = make_tool("research.reserve", world, "crew")
    autogen = make_tool("reviewer.notify", world, "autogen", fail=True)

    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        with pytest.raises(SagaAborted) as excinfo:
            async with saga_scope(wal=wal, name="xfw") as saga:
                await crew_tool(amount=1)
                assert world["crew"] == ["crew_1"]
                await autogen(channel="#x")
        records = await wal.read_all()
    finally:
        await wal.close()

    # The claim under test: a failure on the AutoGen side undid the CrewAI-side
    # effect. That is the cross-framework unwind.
    assert world["crew"] == []
    report = excinfo.value.report
    assert [s.tool for s in report.compensated] == ["research.reserve"]

    # The failing step itself has no inverse in this fixture, so the engine
    # reports it ORPHANED and the rollback as INCOMPLETE. That is correct and
    # asserted deliberately: a test that demanded `clean` here would be
    # demanding the engine flatter a workflow with a genuine gap in it.
    assert not report.clean
    assert [s.tool for s in report.orphaned] == ["reviewer.notify"]

    saga_ids = {r["saga_id"] for r in records if r.get("saga_id")}
    assert len(saga_ids) == 1, "the tools landed in different sagas"


@aio
async def test_a_real_langchain_tool_joins_the_same_saga(tmp_path):
    """The LangChain side is not a stand-in: a real StructuredTool through the
    real adapter, which is what a LangGraph ToolNode invokes."""
    langchain_core = pytest.importorskip("langchain_core")
    from langchain_core.tools import StructuredTool

    from agent_saga.adapters.langgraph import wrap_tool

    world = {"drafts": [], "other": []}

    def publish(title: str) -> dict:
        """Publish a draft."""
        world["drafts"].append(title)
        return {"id": title}

    published = wrap_tool(
        StructuredTool.from_function(publish, name="writer.publish",
                                     description="Publish a draft."),
        semantics=C,
        compensate=lambda r: Compensation(
            fn=lambda t: world["drafts"].remove(t), kwargs={"t": r["id"]},
            description="unpublish"))

    other = make_tool("reviewer.notify", world, "other", fail=True)

    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        with pytest.raises(SagaAborted):
            async with saga_scope(wal=wal, name="xfw") as saga:
                await published.ainvoke({"title": "Q3"})
                assert world["drafts"] == ["Q3"]
                await other(channel="#x")
        records = await wal.read_all()
    finally:
        await wal.close()

    assert world["drafts"] == [], "the LangChain tool's effect was not undone"
    committed = [r["tool"] for r in records if r.get("event") == "STEP_COMMITTED"]
    assert "writer.publish" in committed


@aio
async def test_the_same_wrapped_tool_still_works_outside_a_saga():
    """`build_runner` calls through untouched when no saga is active, so one
    object serves the agent, a script, and a unit test."""
    world = {"crew": []}
    tool = make_tool("research.reserve", world, "crew")
    result = await tool(amount=1)          # no saga_scope
    assert result == {"id": "crew_1"}
    assert world["crew"] == ["crew_1"]     # ran for real, nothing rolled back


@aio
async def test_rollback_order_is_lifo_across_frameworks(tmp_path):
    order = []
    world = {"a": [], "b": [], "c": []}

    def tool(name, key, fail=False):
        async def call(**kwargs):
            if fail:
                raise RuntimeError("boom")
            world[key].append(name)
            return {"id": name}

        return build_runner(
            call, name=name, semantics=C,
            compensate=lambda r: Compensation(
                fn=lambda n: order.append(n), kwargs={"n": name},
                description=f"undo {name}") if r else None)

    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        with pytest.raises(SagaAborted):
            async with saga_scope(wal=wal) as saga:
                await tool("crew.first", "a")()
                await tool("langchain.second", "b")()
                await tool("autogen.third", "c", fail=True)()
    finally:
        await wal.close()

    assert order == ["langchain.second", "crew.first"]


# -- the example itself runs ------------------------------------------------------

def test_the_cross_framework_example_runs_and_ends_clean():
    result = subprocess.run(
        [sys.executable, str(ROOT / "examples" / "cross_framework.py")],
        capture_output=True, text=True, timeout=300, cwd=str(ROOT))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "rollback reported: clean" in result.stdout
    assert "distinct saga ids : 1" in result.stdout
    # every framework's effect is gone from the world afterwards
    tail = result.stdout.split("After:")[1]
    assert "budget_reserved    empty" in tail
    assert "drafts             empty" in tail
