"""ContextBroker: compression that can prove itself, or it doesn't get served.

The claims under test:
1. A summary is admitted only with receipts that resolve at admission time.
2. At every pack, receipts are re-verified: a drifted source means eviction,
   named in the report -- the summary is never served, this pack or any later.
3. Packing is deterministic (same state + budget => byte-identical output) and
   the prefix block is byte-stable as HOT grows.
4. Budgeting is truthful: everything excluded is named with a reason, the
   oldest HOT entries drop first, and an impossible budget refuses loudly.
5. The pack report -- including the output hash -- lands in the WAL, making
   "what did the model see?" answerable from the log alone.
"""

import pytest
from conftest import aio

from agent_saga import AsyncWAL
from agent_saga.context_broker import (
    ContextBroker,
    FileColdStore,
    PackOverflow,
    ProvenanceError,
    Span,
)

DOC = ("SECTION A: the pump pressure limit is 4200 kPa.\n"
       "SECTION B: the retry ceiling is three attempts.\n"
       "SECTION C: operators must sign off on overrides.\n")


def make_broker(**kw) -> ContextBroker:
    return ContextBroker(prefix="You are the on-call assistant.", **kw)


# -- 1. admission requires receipts that resolve --------------------------------

def test_admission_verifies_every_receipt():
    broker = make_broker()
    spans = broker.add_document("runbook", DOC, chunk_chars=48)
    sid = broker.admit_summary("pressure limit 4200 kPa", spans[:1])
    assert sid == "s1"


def test_admission_refuses_a_wrong_hash():
    broker = make_broker()
    broker.add_document("runbook", DOC)
    forged = Span(doc_id="runbook", start=0, end=10, sha256="sha256:" + "0" * 64)
    with pytest.raises(ProvenanceError, match="has changed"):
        broker.admit_summary("forged claim", [forged])


def test_admission_refuses_out_of_range_and_missing_docs():
    broker = make_broker()
    broker.add_document("runbook", DOC)
    with pytest.raises(ProvenanceError, match="out of range"):
        broker.admit_summary("x", [Span("runbook", 0, 10_000, "sha256:" + "0" * 64)])
    with pytest.raises(ProvenanceError, match="not in the cold store"):
        broker.admit_summary("x", [Span("ghost", 0, 1, "sha256:" + "0" * 64)])


def test_admission_refuses_summaries_with_no_receipts():
    broker = make_broker()
    with pytest.raises(ProvenanceError, match="at least one span receipt"):
        broker.admit_summary("an assertion impersonating a document", [])


# -- 2. drift means eviction, named, permanent -----------------------------------

def test_a_drifted_source_evicts_the_summary_at_pack_time():
    broker = make_broker()
    spans = broker.add_document("runbook", DOC)
    sid = broker.admit_summary("limit is 4200 kPa", spans)

    broker.cold.put("runbook", DOC.replace("4200", "9999"))   # source drifts

    packed = broker.pack(budget_tokens=500)
    assert sid not in packed.included
    assert sid in packed.evicted and "has changed" in packed.evicted[sid]
    assert "4200" not in packed.text

    # and it stays gone: the next pack doesn't resurrect it silently
    again = broker.pack(budget_tokens=500)
    assert sid not in again.included and sid not in again.evicted  # already removed


def test_hydrate_returns_the_exact_slice_or_refuses():
    broker = make_broker()
    spans = broker.add_document("runbook", DOC, chunk_chars=48)
    assert broker.hydrate(spans[0]) == DOC[spans[0].start:spans[0].end]

    broker.cold.put("runbook", DOC + "appended audit note\n")  # tail growth is
    assert broker.hydrate(spans[0])                            # fine for old spans

    broker.cold.put("runbook", DOC.upper())                    # content drift is not
    with pytest.raises(ProvenanceError, match="has changed"):
        broker.hydrate(spans[0])


# -- 3. determinism and prefix stability ------------------------------------------

def test_packing_is_deterministic():
    def build():
        b = make_broker()
        spans = b.add_document("runbook", DOC, chunk_chars=48)
        b.admit_summary("pressure limit", spans[:1])
        b.admit_summary("retry ceiling", spans[1:2])
        b.push_hot("user: what is the limit?")
        return b

    one, two = build().pack(400), build().pack(400)
    assert one.text == two.text
    assert one.content_hash == two.content_hash
    assert one.describe() == two.describe()


def test_the_prefix_block_is_byte_stable_as_hot_grows():
    broker = make_broker()
    spans = broker.add_document("runbook", DOC)
    broker.admit_summary("the whole runbook, condensed", spans)
    broker.push_hot("turn 1")
    first = broker.pack(1000)

    broker.push_hot("turn 2")
    second = broker.pack(1000)
    assert second.text.startswith(first.text)   # cache-friendly: append-only


# -- 4. truthful budgeting ----------------------------------------------------------

def test_exclusions_are_named_and_oldest_hot_drops_first():
    broker = ContextBroker(prefix="P")
    old = broker.push_hot("OLD " * 40)
    new = broker.push_hot("NEW " * 40)

    packed = broker.pack(budget_tokens=60)      # room for one of the two
    assert new in packed.included
    assert old in packed.excluded
    assert "older HOT entries are dropped first" in packed.excluded[old]
    assert packed.estimated_tokens <= packed.budget_tokens


def test_an_impossible_budget_refuses_instead_of_truncating():
    broker = ContextBroker(prefix="a mandatory preamble " * 50)
    with pytest.raises(PackOverflow, match="prefix alone"):
        broker.pack(budget_tokens=10)


# -- 5. the pack lands in the WAL ------------------------------------------------------

@aio
async def test_what_the_model_saw_is_answerable_from_the_wal(tmp_path):
    wal = AsyncWAL(tmp_path / "wal.jsonl")
    await wal.start()
    try:
        broker = make_broker(wal=wal)
        spans = broker.add_document("runbook", DOC)
        broker.admit_summary("condensed runbook", spans)
        packed = broker.pack(500)
        await wal.barrier()
        events = [r for r in await wal.read_all() if r.get("event") == "CONTEXT_PACKED"]
    finally:
        await wal.close()

    assert len(events) == 1
    assert events[0]["content_hash"] == packed.content_hash
    assert events[0]["included"] == list(packed.included)


# -- COLD at scale: the small-model-over-huge-docs path ---------------------------------

def test_file_cold_store_carries_a_document_far_beyond_any_context_window(tmp_path):
    broker = ContextBroker(prefix="P", cold=FileColdStore(tmp_path / "cold"))
    huge = "\n".join(f"telemetry line {i}: nominal" for i in range(20_000))
    spans = broker.add_document("telemetry", huge, chunk_chars=4000)
    assert len(spans) > 50                       # far beyond a small window

    sid = broker.admit_summary("all channels nominal", spans[:3])
    packed = broker.pack(200)
    assert sid in packed.included
    assert len(packed.text) < 1000               # the model sees kilobytes,
    assert broker.hydrate(spans[7])              # the receipts reach megabytes
