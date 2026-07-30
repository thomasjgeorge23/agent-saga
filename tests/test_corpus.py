"""The WAL as a labelled corpus, with blame attributed correctly.

The naive version of this feature poisons the dataset it is trying to build.
When step 5 fails, steps 1-4 are rolled back -- and they were *right*. An
exporter that labels all four negative teaches the model to avoid the calls that
worked. So the attribution logic is what these tests are mostly about:

1. A step rolled back because a LATER step failed is COLLATERAL, not REJECTED,
   and is excluded from the trainable set.
2. The step that actually failed is the only REJECTED one.
3. An UNKNOWN outcome is AMBIGUOUS and excluded -- guessing would put noise in
   the corpus and call it signal.
4. ORPHANED is a flag about compensation coverage, never a label about whether
   the call was correct.
5. Exporting real customer data requires acknowledging that is what it is.
"""

import json

import pytest
from conftest import aio

from agent_saga import ActionSemantics, AsyncWAL, Compensation, SagaAborted, saga_scope
from agent_saga.corpus import Corpus, Label, build_corpus

C = ActionSemantics.COMPENSABLE


def comp(**kwargs):
    return lambda r: Compensation(fn=lambda: None, description="undo")


async def happy_saga(wal):
    """Two steps, both committed, saga completes -> both ACCEPTED."""
    async with saga_scope(wal=wal, name="happy") as saga:
        await saga.execute(tool="stripe.charge", semantics=C,
                           forward=lambda amount: {"id": "ch_1"},
                           forward_kwargs={"amount": 4200}, compensate=comp())
        await saga.execute(tool="ship.order", semantics=C,
                           forward=lambda sku: {"id": "s1"},
                           forward_kwargs={"sku": "widget"}, compensate=comp())


async def failing_saga(wal):
    """Step 1 commits, step 2 raises -> step 1 COLLATERAL, step 2 AMBIGUOUS."""
    with pytest.raises(SagaAborted):
        async with saga_scope(wal=wal, name="sad") as saga:
            await saga.execute(tool="stripe.charge", semantics=C,
                               forward=lambda amount: {"id": "ch_2"},
                               forward_kwargs={"amount": 999}, compensate=comp())

            def boom(**kw):
                raise ConnectionError("carrier down")

            await saga.execute(tool="ship.order", semantics=C, forward=boom,
                               forward_kwargs={"sku": "gizmo"}, compensate=comp())


# -- 1 & 2. blame attribution ------------------------------------------------------

@aio
async def test_a_step_rolled_back_for_a_later_failure_is_collateral(tmp_path):
    """The correctness heart of the module. Labelling this REJECTED would teach
    a model to stop making the charge that worked."""
    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        await failing_saga(wal)
        records = await wal.read_all()
    finally:
        await wal.close()

    corpus = build_corpus(records)
    charge = next(e for e in corpus.examples if e.tool == "stripe.charge")

    assert charge.label is Label.COLLATERAL
    assert not charge.trainable
    assert "later step failed" in charge.reason
    assert "action itself was accepted" in charge.reason


@aio
async def test_the_step_that_failed_is_the_ambiguous_one_not_the_earlier_ones(tmp_path):
    """A raised call is UNKNOWN -- it may still have landed -- so it is
    ambiguous rather than a clean negative."""
    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        await failing_saga(wal)
        records = await wal.read_all()
    finally:
        await wal.close()

    corpus = build_corpus(records)
    ship = next(e for e in corpus.examples if e.tool == "ship.order")
    assert ship.label is Label.AMBIGUOUS
    assert not ship.trainable
    assert "may still have landed" in ship.reason


@aio
async def test_a_completed_saga_yields_accepted_examples(tmp_path):
    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        await happy_saga(wal)
        records = await wal.read_all()
    finally:
        await wal.close()

    corpus = build_corpus(records)
    assert len(corpus.examples) == 2
    assert all(e.label is Label.ACCEPTED for e in corpus.examples)
    assert len(corpus.trainable) == 2
    assert corpus.counts["accepted"] == 2


@aio
async def test_a_step_whose_intent_was_written_but_never_committed_is_rejected(tmp_path):
    """No STEP_COMMITTED and no STEP_UNKNOWN means the call did not get as far
    as returning -- the clearest negative the log offers."""
    records = [
        {"saga_id": "s1", "event": "SAGA_START", "name": "x"},
        {"saga_id": "s1", "event": "STEP_INTENT", "step_id": "a",
         "tool": "email.send", "kwargs": {"to": "x@y.com"}},
        {"saga_id": "s1", "event": "SAGA_ABORTED"},
    ]
    corpus = build_corpus(records)
    example = corpus.examples[0]
    assert example.label is Label.REJECTED
    assert example.trainable
    assert "the call that failed" in example.reason


# -- 3 & 4. ambiguity and orphaning ---------------------------------------------------

@aio
async def test_a_saga_with_no_terminal_record_is_ambiguous(tmp_path):
    """A crashed process leaves committed steps whose outcome nobody knows."""
    records = [
        {"saga_id": "s1", "event": "SAGA_START", "name": "x"},
        {"saga_id": "s1", "event": "STEP_INTENT", "step_id": "a",
         "tool": "stripe.charge", "kwargs": {"amount": 1}},
        {"saga_id": "s1", "event": "STEP_COMMITTED", "step_id": "a",
         "tool": "stripe.charge"},
        # no SAGA_COMPLETE / SAGA_ABORTED: the process died
    ]
    example = build_corpus(records).examples[0]
    assert example.label is Label.AMBIGUOUS
    assert "process died" in example.reason


def test_orphaned_is_a_flag_not_a_label():
    """Being unrecoverable is a fact about compensation coverage, not about
    whether the call was correct."""
    records = [
        {"saga_id": "s1", "event": "SAGA_START"},
        {"saga_id": "s1", "event": "STEP_INTENT", "step_id": "a",
         "tool": "email.send", "kwargs": {"to": "x@y.com"}},
        {"saga_id": "s1", "event": "STEP_COMMITTED", "step_id": "a",
         "tool": "email.send"},
        {"saga_id": "s1", "event": "STEP_ORPHANED", "step_id": "a",
         "tool": "email.send"},
        {"saga_id": "s1", "event": "SAGA_ABORTED"},
    ]
    example = build_corpus(records).examples[0]
    assert example.orphaned is True
    assert example.label is Label.COLLATERAL       # not REJECTED


def test_a_gate_refusal_is_surfaced_but_not_invented_as_an_example():
    """The gate raises before STEP_INTENT, so no step was ever logged for the
    call it stopped. Fabricating one would be inventing data."""
    records = [
        {"saga_id": "s1", "event": "SAGA_START"},
        {"saga_id": "s1", "event": "SAGA_ABORT_CAUSE",
         "cause_type": "PreFlightViolation", "cause": "over budget"},
        {"saga_id": "s1", "event": "SAGA_ABORTED"},
    ]
    corpus = build_corpus(records)
    assert corpus.examples == ()
    assert corpus.gate_refusals == ("s1: aborted by PreFlightViolation",)
    assert "gate-refused sagas" in corpus.format_text()


# -- the training-shaped outputs -------------------------------------------------------

@aio
async def test_preference_pairs_are_matched_within_a_tool(tmp_path):
    """'A good charge versus a bad email' teaches nothing about either."""
    records = [
        {"saga_id": "good", "event": "SAGA_START"},
        {"saga_id": "good", "event": "STEP_INTENT", "step_id": "a",
         "tool": "stripe.charge", "kwargs": {"amount": 4200}},
        {"saga_id": "good", "event": "STEP_COMMITTED", "step_id": "a",
         "tool": "stripe.charge"},
        {"saga_id": "good", "event": "SAGA_COMPLETE"},
        {"saga_id": "bad", "event": "SAGA_START"},
        {"saga_id": "bad", "event": "STEP_INTENT", "step_id": "b",
         "tool": "stripe.charge", "kwargs": {"amount": 999999}},
        {"saga_id": "bad", "event": "SAGA_ABORTED"},
    ]
    corpus = build_corpus(records)
    pairs = corpus.preference_pairs()

    assert len(pairs) == 1
    accepted, rejected = pairs[0]
    assert accepted.tool == rejected.tool == "stripe.charge"
    assert accepted.label is Label.ACCEPTED and rejected.label is Label.REJECTED
    assert accepted.arguments["amount"] == 4200
    assert rejected.arguments["amount"] == 999999


@aio
async def test_examples_link_to_the_context_the_model_saw(tmp_path):
    """An action with no prompt is far weaker training signal, so the link is
    reported rather than assumed."""
    records = [
        {"saga_id": "s1", "event": "SAGA_START"},
        {"saga_id": "s1", "event": "AGENT_DECISION", "step": 1,
         "tool": "stripe.charge", "context_hash": "sha256:abc"},
        {"saga_id": "s1", "event": "STEP_INTENT", "step_id": "a",
         "tool": "stripe.charge", "kwargs": {"amount": 1}},
        {"saga_id": "s1", "event": "STEP_COMMITTED", "step_id": "a",
         "tool": "stripe.charge"},
        {"saga_id": "s1", "event": "SAGA_COMPLETE"},
    ]
    corpus = build_corpus(records)
    assert corpus.examples[0].context_hash == "sha256:abc"
    assert len(corpus.with_context) == 1


@aio
async def test_a_log_of_only_successes_has_nothing_trainable(tmp_path):
    """Said out loud, because a corpus of all-positives silently trains
    nothing and looks fine."""
    wal = AsyncWAL(tmp_path / "w.wal")
    await wal.start()
    try:
        await happy_saga(wal)
        records = await wal.read_all()
    finally:
        await wal.close()

    corpus = build_corpus(records)
    assert corpus.preference_pairs() == ()          # no negatives to pair


def test_an_empty_log_reports_no_signal():
    corpus = build_corpus([])
    assert corpus.examples == ()
    assert "Nothing trainable here" in corpus.format_text()


# -- 5. privacy ---------------------------------------------------------------------------

def test_export_refuses_without_an_explicit_acknowledgement(tmp_path):
    """This file will contain real customer data and will outlive the system
    that made it. The awkward argument is the point."""
    records = [
        {"saga_id": "s1", "event": "SAGA_START"},
        {"saga_id": "s1", "event": "STEP_INTENT", "step_id": "a",
         "tool": "email.send", "kwargs": {"customer_email": "ada@example.com"}},
        {"saga_id": "s1", "event": "STEP_COMMITTED", "step_id": "a",
         "tool": "email.send"},
        {"saga_id": "s1", "event": "SAGA_COMPLETE"},
    ]
    corpus = build_corpus(records)
    with pytest.raises(PermissionError, match="acknowledgement"):
        corpus.to_jsonl(tmp_path / "out.jsonl")
    assert not (tmp_path / "out.jsonl").exists()


def test_redaction_matches_on_substring(tmp_path):
    """`email` must cover `customer_email`; a clever matcher that missed a
    field would be worse than an obvious one."""
    records = [
        {"saga_id": "s1", "event": "SAGA_START"},
        {"saga_id": "s1", "event": "STEP_INTENT", "step_id": "a",
         "tool": "email.send",
         "kwargs": {"customer_email": "ada@example.com", "template": "welcome"}},
        {"saga_id": "s1", "event": "STEP_COMMITTED", "step_id": "a",
         "tool": "email.send"},
        {"saga_id": "s1", "event": "SAGA_COMPLETE"},
    ]
    target = tmp_path / "out.jsonl"
    written = build_corpus(records).to_jsonl(
        target, redact=["email"], i_understand_this_contains_real_data=True)

    assert written == 1
    row = json.loads(target.read_text(encoding="utf-8").strip())
    assert row["arguments"]["customer_email"] == "[REDACTED]"
    assert row["arguments"]["template"] == "welcome"        # untouched


def test_only_trainable_examples_are_exported(tmp_path):
    wal_records = [
        {"saga_id": "s1", "event": "SAGA_START"},
        {"saga_id": "s1", "event": "STEP_INTENT", "step_id": "a",
         "tool": "t", "kwargs": {}},
        {"saga_id": "s1", "event": "STEP_COMMITTED", "step_id": "a", "tool": "t"},
        {"saga_id": "s1", "event": "COMPENSATED", "step_id": "a", "tool": "t"},
        {"saga_id": "s1", "event": "SAGA_ABORTED"},
    ]
    corpus = build_corpus(wal_records)
    assert corpus.examples[0].label is Label.COLLATERAL

    target = tmp_path / "out.jsonl"
    assert corpus.to_jsonl(target, i_understand_this_contains_real_data=True) == 0
    assert target.read_text(encoding="utf-8") == ""


def test_the_report_is_machine_readable(tmp_path):
    records = [
        {"saga_id": "s1", "event": "SAGA_START"},
        {"saga_id": "s1", "event": "STEP_INTENT", "step_id": "a",
         "tool": "t", "kwargs": {}},
        {"saga_id": "s1", "event": "STEP_COMMITTED", "step_id": "a", "tool": "t"},
        {"saga_id": "s1", "event": "SAGA_COMPLETE"},
    ]
    data = build_corpus(records).describe()
    assert data["sagas"] == 1
    assert data["counts"]["accepted"] == 1
    assert data["trainable"] == 1
