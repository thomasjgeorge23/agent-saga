"""One saga across three frameworks. One rollback across all of them.

    python examples/cross_framework.py

The multi-agent problem nobody has an answer for: a CrewAI researcher writes
something, a LangGraph writer publishes it, an AutoGen reviewer sends the
notification -- and the notification fails. Three frameworks, three tool
protocols, three separate notions of state. Which of them un-writes the draft?

None of them, individually. They cannot: each one only knows about its own
graph. So the cleanup falls to whoever is on call, armed with three dashboards
and a guess.

agent-saga's answer is that the boundary does not belong to any framework.
`saga_scope` sets a **contextvar**, and every adapter's runner
(`adapters/_common.build_runner`) checks `current_saga()` before it does
anything. A wrapped tool joins whatever saga is active, whichever framework
called it -- and outside a saga it calls straight through, so the same object
still works in a unit test. Nothing is registered, negotiated, or synchronised
between frameworks, which is why it works with frameworks that have never
heard of each other.

What is real here, and what is standing in:

  * The **LangChain/LangGraph** tool is genuinely real -- a `langchain_core`
    `StructuredTool` wrapped by `adapters.langgraph.wrap_tool`, the same call
    a LangGraph `ToolNode` would make.
  * The **CrewAI** and **AutoGen** tools use `adapters._common.build_runner`
    (with the sync-to-thread bridge from `adapters/crewai.py`), which is the
    exact routing core those adapters run. Only the framework's own base class
    is stood in, because crewai and autogen are not installed here. Installing
    them and calling `adapters.crewai.wrap_tool` changes the packaging, not the
    behaviour this demo is about.

Nothing touches a network.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict

from agent_saga import ActionSemantics, Compensation, saga_scope
from agent_saga.adapters._common import build_runner
from agent_saga.context import SagaAborted
from agent_saga.wal.file_wal import FileWAL

C = ActionSemantics.COMPENSABLE

# The shared world all three frameworks touch. In a real system these are three
# different systems of record -- which is exactly why no single framework can
# clean up after the others.
WORLD: Dict[str, Any] = {"budget_reserved": [], "drafts": [], "notifications": []}


def show(title: str) -> None:
    print(f"\n  {title}")
    for key, value in WORLD.items():
        marker = "  " if not value else "->"
        print(f"    {marker} {key:<18} {value or 'empty'}")


# -- 1. a CrewAI-shaped tool (synchronous _run, bridged to a thread) --------------

class CrewAIStyleTool:
    """The shape adapters/crewai.py expects: a sync `_run`. CrewAI is not
    installed here, so this stands in for `crewai.tools.BaseTool`."""

    name = "research.reserve_budget"

    def _run(self, amount: int) -> dict:
        entry = {"id": f"res_{len(WORLD['budget_reserved']) + 1}", "amount": amount}
        WORLD["budget_reserved"].append(entry)
        return entry


def release_budget(reservation_id: str) -> dict:
    WORLD["budget_reserved"] = [
        r for r in WORLD["budget_reserved"] if r["id"] != reservation_id]
    return {"released": reservation_id}


def crewai_tool():
    tool = CrewAIStyleTool()

    async def call(**kwargs: Any) -> Any:
        # adapters/crewai.py runs the sync tool on a worker thread so it never
        # blocks the event loop. Same bridge, same reason.
        return await asyncio.to_thread(lambda: tool._run(**kwargs))

    return build_runner(
        call, name=tool.name, semantics=C,
        compensate=lambda r: Compensation(
            fn=release_budget, kwargs={"reservation_id": r["id"]},
            description=f"release {r['id']}"))


# -- 2. a real LangChain tool, wrapped by the real adapter -------------------------

def publish_draft(title: str) -> dict:
    """Publish a draft document."""
    entry = {"id": f"doc_{len(WORLD['drafts']) + 1}", "title": title}
    WORLD["drafts"].append(entry)
    return entry


def unpublish_draft(doc_id: str) -> dict:
    WORLD["drafts"] = [d for d in WORLD["drafts"] if d["id"] != doc_id]
    return {"unpublished": doc_id}


def langchain_tool():
    from langchain_core.tools import StructuredTool

    from agent_saga.adapters.langgraph import wrap_tool

    base = StructuredTool.from_function(
        publish_draft, name="writer.publish_draft",
        description="Publish a draft document.")

    return wrap_tool(
        base, semantics=C,
        compensate=lambda r: Compensation(
            fn=unpublish_draft, kwargs={"doc_id": r["id"]},
            description=f"unpublish {r['id']}"))


# -- 3. an AutoGen-shaped tool that fails ---------------------------------------------

def retract_notification(channel: str) -> dict:
    WORLD["notifications"] = [n for n in WORLD["notifications"] if n != channel]
    return {"retracted": channel}


def autogen_tool():
    async def call(**kwargs: Any) -> Any:
        raise ConnectionError("notification service unreachable")

    # The call raises, so the engine records UNKNOWN: a request that timed out
    # is not a request that did not happen, and the notification may have gone
    # out anyway. An idempotent inverse covers that. Omitting it is legal and
    # the engine says so out loud -- it reports the step ORPHANED and the
    # rollback PARTIAL, which is honest and not what this demo is claiming.
    return build_runner(
        call, name="reviewer.notify", semantics=C,
        compensate=lambda _r: Compensation(
            fn=retract_notification, kwargs={"channel": "#launch"},
            description="retract the notification if it went out"))


# -- the run --------------------------------------------------------------------------

async def main() -> None:
    import tempfile
    from pathlib import Path

    print(__doc__.split("What is real here")[0].rstrip())
    print("=" * 68)

    reserve = crewai_tool()
    publish = langchain_tool()
    notify = autogen_tool()

    wal_path = Path(tempfile.mkdtemp(prefix="agent-saga-xfw-")) / "x.wal"
    wal = FileWAL(wal_path)
    await wal.start()
    try:
        show("Before:")
        print("\n  Running one saga across three frameworks...\n")
        try:
            async with saga_scope(wal=wal, name="cross-framework") as saga:
                r = await reserve(amount=5000)
                print(f"    [CrewAI]    reserved budget    -> {r['id']}")

                # A real LangChain tool. LangGraph would call `ainvoke`; the
                # wrapper routes it into the SAME saga the CrewAI tool joined.
                d = await publish.ainvoke({"title": "Q3 market analysis"})
                print(f"    [LangChain] published draft   -> {d['id']}")

                await notify(channel="#launch")
                print("    [AutoGen]   sent notification")
        except SagaAborted as aborted:
            print(f"    [AutoGen]   FAILED: {aborted.cause}")
            print("\n  One boundary unwinds all three, last in first out:")
            for step in aborted.report.compensated:
                print(f"    <- {step.tool}")
            print(f"\n  rollback reported: "
                  f"{'clean' if aborted.report.clean else 'PARTIAL'}")

        show("After:")

        records = await wal.read_all()
        tools = [r["tool"] for r in records if r.get("event") == "STEP_COMMITTED"]
        saga_ids = {r.get("saga_id") for r in records if r.get("saga_id")}
        print(f"\n  The log proves it was one transaction, not three:")
        print(f"    distinct saga ids : {len(saga_ids)}")
        print(f"    tools committed   : {tools}")
    finally:
        await wal.close()

    print("\n" + "=" * 68)
    print("""  No framework knew about the others. The boundary is a contextvar,
  so each adapter's runner joined whatever saga was already open -- and
  one failure unwound work done through three different tool protocols.

  Draw it:  agent-saga graph --wal %s
""" % wal_path)


if __name__ == "__main__":
    asyncio.run(main())
