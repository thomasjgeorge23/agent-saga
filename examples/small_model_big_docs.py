"""Small model, big documents: the whole harness in one run.

    python examples/small_model_big_docs.py     # no network needed

A "7B-class" host (scripted here so the demo is deterministic and offline --
swap in a real Ollama adapter and nothing else changes) reviews a telemetry
file ~40x larger than its context window, pulls the EXACT source lines behind
a suspicious summary before acting on it, and files a report inside a
transaction. Then the same goal runs against a failing disk, and the
transaction unwinds everything.

What to watch for in the output:
  * the model never sees more than its budget -- but every summary it does see
    carries receipts into the full document, and it verifies one before acting;
  * every decision in the WAL is pinned to the hash of the exact context that
    produced it;
  * when the report write fails, the attempt leaves NOTHING behind.

The model here is scripted from ground truth because this demo is about the
harness, not the model: the loop, receipts, routing, and rollback behave
identically under a real host -- a real 7B is simply sometimes wrong, which is
exactly why the receipts and the rollback exist.
"""

import asyncio
import json
import tempfile
from pathlib import Path

from agent_saga import ActionSemantics, AsyncWAL, Compensation, SagaAborted, saga_scope
from agent_saga.agent_loop import AgentLoop, LoopTool
from agent_saga.context_broker import ContextBroker, FileColdStore
from agent_saga.ir import Capabilities, ChatRequest, ChatResponse, CostClass
from agent_saga.router import Router

WORK = Path(tempfile.mkdtemp(prefix="agent-saga-demo-"))
CHUNK = 4000


# -- a deterministic stand-in for a small local model ---------------------------------

class ScriptedSmallHost:
    """Context window: 2048 tokens -- the document will not fit, on purpose."""

    def __init__(self, replies):
        self.replies = list(replies)

    @property
    def name(self):
        return "local-7b"

    def capabilities(self):
        return Capabilities(context_tokens=2048, cost_class=CostClass.LOCAL)

    async def complete(self, request: ChatRequest) -> ChatResponse:
        return ChatResponse(text=self.replies.pop(0), provider="local-7b",
                            model="scripted-7b")


def tool_action(name, **args):
    return json.dumps({"action": "tool", "tool": name, "args": args})


def final(answer):
    return json.dumps({"action": "final", "answer": answer})


# -- build the document pack ------------------------------------------------------------

def build_telemetry() -> str:
    lines = [f"t+{i:05d}s ch{i % 7}: nominal" for i in range(20_000)]
    lines[13_337] = "t+13337s ch3: WARNING pressure 4390 kPa exceeds 4200 kPa limit"
    return "\n".join(lines)


def admit_flagged_summaries(broker, spans, text):
    """A deliberately dumb summarizer (grep, basically) -- in production this is
    an LLM call through the same Router. Only the chunks worth the model's
    budget are admitted to WARM, and only with receipts; the all-nominal rest
    stays in COLD, one `read_source` call away."""
    flagged = {}
    for i, span in enumerate(spans):
        warnings = [l for l in text[span.start:span.end].splitlines() if "WARNING" in l]
        if warnings:
            flagged[i] = broker.admit_summary(
                f"chunk {i}: {len(warnings)} warning(s): {'; '.join(warnings)}",
                [span])
    return flagged


# -- the run -------------------------------------------------------------------------------

async def review(fail_the_disk: bool) -> None:
    text = build_telemetry()
    report_path = WORK / ("report-fail.txt" if fail_the_disk else "report.txt")

    wal = AsyncWAL(WORK / ("wal-fail.jsonl" if fail_the_disk else "wal.jsonl"))
    await wal.start()
    try:
        broker = ContextBroker(prefix="You review pump telemetry for anomalies.",
                               cold=FileColdStore(WORK / "cold"), wal=wal)
        spans = broker.add_document("telemetry", text, chunk_chars=CHUNK)
        flagged = admit_flagged_summaries(broker, spans, text)
        broker.push_hot(f"SCAN: {len(spans)} chunks summarized; "
                        f"{len(flagged)} flagged (receipted above); rest nominal.")
        anomaly_chunk = next(iter(flagged))

        def read_source(chunk: int):
            """The receipt, exercised: hand back the EXACT verified source slice."""
            return broker.hydrate(spans[chunk])

        def file_report(summary: str):
            if fail_the_disk:
                raise OSError("disk full")            # simulate the bad day
            report_path.write_text(summary, encoding="utf-8")
            return {"path": str(report_path)}

        tools = [
            LoopTool(name="read_source",
                     description="fetch the exact, hash-verified source text of a chunk",
                     parameters={"chunk": "int"}, fn=read_source,
                     semantics=ActionSemantics.REVERSIBLE,
                     compensate=lambda r: Compensation(fn=lambda: None,
                                                       description="read-only")),
            # The compensation uses the pre-known path, NOT the forward result:
            # on an UNKNOWN outcome (write raised -- did it land?) the factory
            # receives None, and an inverse derived from pre-state stays valid
            # either way. unlink(missing_ok=True) is idempotent on both answers.
            LoopTool(name="file_report",
                     description="file the anomaly report",
                     parameters={"summary": "string"}, fn=file_report,
                     semantics=ActionSemantics.COMPENSABLE,
                     compensate=lambda _r: Compensation(
                         fn=lambda path: Path(path).unlink(missing_ok=True),
                         kwargs={"path": str(report_path)},
                         description="withdraw the filed report")),
        ]

        host = ScriptedSmallHost([
            tool_action("read_source", chunk=anomaly_chunk),
            tool_action("file_report",
                        summary="Overpressure at t+13337s on ch3: 4390 kPa vs 4200 limit."),
            final("Filed: one overpressure anomaly at t+13337s, verified against source."),
        ])
        # Budget sized for the host: 1200 packed tokens x the router's 1.25
        # safety margin stays inside the declared 2048 window. Get this wrong
        # and the router refuses with the exact arithmetic -- run A of this
        # demo's first draft did exactly that, which is the feature.
        loop = AgentLoop(Router([host]), broker, tools,
                         max_steps=5, budget_tokens=1200)

        print(f"\n=== run {'B: failing disk' if fail_the_disk else 'A: healthy'} ===")
        print(f"document: {len(text):,} chars; model window: 2048 tokens "
              f"(~{len(text) // (2048 * 4)}x too big)")
        try:
            async with saga_scope(wal=wal) as ctx:
                result = await loop.run("Review the telemetry and file a report "
                                        "for any anomaly.", ctx)
            print(f"COMPLETED: {result.answer}")
            print(f"report on disk: {report_path.read_text('utf-8')!r}")
        except SagaAborted as aborted:
            print(f"ABORTED cleanly: {aborted.args[0]!r}")
            print(f"report on disk after rollback: exists={report_path.exists()}")

        records = await wal.read_all()
        chain = [r["event"] for r in records]
        print("WAL: " + " -> ".join(chain))
        for r in records:
            if r["event"] == "AGENT_DECISION":
                print(f"  step {r['step']}: {r['action']}"
                      f"{' ' + r['tool'] if r['tool'] else ''}  "
                      f"(saw context {r['context_hash'][:23]}...)")
    finally:
        await wal.close()


async def main():
    await review(fail_the_disk=False)
    await review(fail_the_disk=True)
    print(f"\nartifacts kept for inspection under: {WORK}")


if __name__ == "__main__":
    asyncio.run(main())
