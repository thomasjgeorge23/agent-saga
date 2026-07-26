"""Grounding: a hallucination cannot pose as a sourced fact.

The claims under test:
1. A claim citing a live summary whose receipts resolve is VERIFIED; a claim
   with no citation is UNCITED -- labeled, never silently trusted.
2. Broken evidence is named precisely: a citation to a never-admitted id, an
   evicted summary (with its eviction reason), or receipts that no longer
   resolve, each produce BROKEN_CITATION with the exact cause.
3. A direct quote must appear in the cited sources, or the claim is
   BROKEN_QUOTE -- the strongest mechanical entailment proxy.
4. The optional entailment hook can downgrade a structurally-sound claim to
   UNSUPPORTED; without a hook, the answer's basis says "structural".
5. fully_grounded is strict (every claim VERIFIED), and the ANSWER_GROUNDED
   WAL event extends the audit chain to the answer itself.
"""

import pytest
from conftest import aio

from agent_saga import AsyncWAL
from agent_saga.context_broker import ContextBroker
from agent_saga.grounding import ground

DOC = ("The pump pressure limit is 4200 kPa. "
       "Operators must sign off on all overrides. "
       "The retry ceiling is three attempts.")


def make_broker(**kw):
    broker = ContextBroker(prefix="P", **kw)
    spans = broker.add_document("runbook", DOC)
    sid = broker.admit_summary("limits and override policy", spans)
    return broker, sid


# -- 1. verified vs. labeled ---------------------------------------------------------

def test_cited_and_uncited_claims_are_classified_not_trusted():
    broker, sid = make_broker()
    answer = (f"The pressure limit is 4200 kPa [{sid}]. "
              f"I believe the system was installed in 2019.")

    result = ground(answer, broker)
    assert result.claims[0].status == "VERIFIED"
    assert result.claims[0].citations == (sid,)
    assert result.claims[1].status == "UNCITED"
    assert not result.fully_grounded
    assert result.counts == {"VERIFIED": 1, "UNCITED": 1}


def test_multiple_citations_on_one_claim():
    broker, s1 = make_broker()
    spans = broker.add_document("addendum", "Overrides require two signatures.")
    s2 = broker.admit_summary("override addendum", spans)

    result = ground(f"Overrides need sign-off and two signatures [{s1},{s2}].", broker)
    assert result.claims[0].status == "VERIFIED"
    assert result.claims[0].citations == (s1, s2)
    assert result.fully_grounded


# -- 2. broken evidence is named precisely ----------------------------------------------

def test_a_citation_to_a_summary_that_never_existed():
    broker, _ = make_broker()
    result = ground("The limit is 9999 kPa [s99].", broker)
    assert result.claims[0].status == "BROKEN_CITATION"
    assert "not an admitted summary" in result.claims[0].detail


def test_a_citation_to_an_evicted_summary_carries_the_eviction_reason():
    broker, sid = make_broker()
    broker.cold.put("runbook", DOC.replace("4200", "9999"))   # source drifts
    broker.pack(500)                                          # eviction happens here

    result = ground(f"The limit is 4200 kPa [{sid}].", broker)
    assert result.claims[0].status == "BROKEN_CITATION"
    assert "gone" in result.claims[0].detail
    assert "has changed" in result.claims[0].detail           # the WHY travels


def test_receipts_that_stopped_resolving_break_the_citation():
    broker, sid = make_broker()
    broker.cold.put("runbook", DOC.replace("4200", "9999"))   # drift, but no pack:
    result = ground(f"The limit is 4200 kPa [{sid}].", broker)  # summary still WARM
    assert result.claims[0].status == "BROKEN_CITATION"
    assert "no longer resolve" in result.claims[0].detail


# -- 3. quotes must exist in the sources ---------------------------------------------------

def test_a_quote_present_in_the_source_verifies():
    broker, sid = make_broker()
    result = ground(f'The rule is "Operators must sign off on all overrides." [{sid}]',
                    broker)
    assert result.claims[0].status == "VERIFIED"


def test_an_invented_quote_is_broken_even_with_a_valid_citation():
    broker, sid = make_broker()
    result = ground(f'The manual says "pressure may exceed limits briefly" [{sid}].',
                    broker)
    assert result.claims[0].status == "BROKEN_QUOTE"
    assert "appears in none" in result.claims[0].detail


# -- 4. the entailment hook ------------------------------------------------------------------

def test_the_entailment_hook_can_reject_a_structurally_sound_claim():
    broker, sid = make_broker()
    answer = f"Therefore the pump can safely run at 5000 kPa [{sid}]."

    permissive = ground(answer, broker)
    assert permissive.claims[0].status == "VERIFIED"
    assert permissive.basis == "structural"                   # and says so

    strict = ground(answer, broker,
                    entailment=lambda claim, sources: "5000" in sources)
    assert strict.claims[0].status == "UNSUPPORTED"
    assert strict.basis == "structural+entailment"


# -- 5. strictness and the audit chain ----------------------------------------------------------

def test_fully_grounded_is_strict():
    broker, sid = make_broker()
    result = ground(f"Limit is 4200 kPa [{sid}]. Probably fine to exceed it.", broker)
    assert not result.fully_grounded                          # one UNCITED spoils it


def test_the_annotated_answer_wears_its_labels():
    broker, sid = make_broker()
    result = ground(f"Limit is 4200 kPa [{sid}]. Installed in 2019, I think.", broker)
    annotated = result.format_annotated()
    assert f"[VERIFIED:{sid}]" in annotated
    assert "[UNCITED]" in annotated


@aio
async def test_the_grounding_verdict_lands_in_the_wal(tmp_path):
    wal = AsyncWAL(tmp_path / "wal.jsonl")
    await wal.start()
    try:
        broker = ContextBroker(prefix="P", wal=wal)
        spans = broker.add_document("runbook", DOC)
        sid = broker.admit_summary("limits", spans)

        result = ground(f"Limit is 4200 kPa [{sid}]. Trust me on the rest.", broker)
        await wal.barrier()
        events = [r for r in await wal.read_all()
                  if r.get("event") == "ANSWER_GROUNDED"]
    finally:
        await wal.close()

    assert len(events) == 1
    assert events[0]["content_hash"] == result.content_hash
    assert events[0]["counts"] == {"VERIFIED": 1, "UNCITED": 1}
