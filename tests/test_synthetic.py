"""Synthetic WALs: the shape of real traffic, none of its contents.

Two properties matter more than the generator itself, and both are load
bearing:

1. **No real value escapes.** The point is to share a log you could not
   otherwise share. If a customer's email survives into the synthetic output,
   the feature is worse than useless -- it is a leak wearing the word
   "synthetic".
2. **Everything generated is marked.** An audit log indistinguishable from a
   real one is an instrument for fabricating evidence, not a test fixture.

After that: the output must be structurally valid, so the tools it exists to
exercise (`verify`, `certify`, `build_corpus`, `wal_to_mermaid`, the recovery
daemon) accept it for the right reasons.
"""

import json

import pytest
from conftest import aio

from agent_saga import ActionSemantics, AsyncWAL, Compensation, SagaAborted, saga_scope
from agent_saga.synthetic import (
    SYNTHETIC_FIELD,
    WALProfile,
    is_synthetic,
    synthesize,
)

C = ActionSemantics.COMPENSABLE

# Values a real log would hold and a synthetic one must never reproduce.
SECRET_EMAIL = "ada.lovelace@private-bank.example"
SECRET_NOTE = "wire to account 90210 before friday"
SECRET_AMOUNT = 4242424


async def real_traffic(wal):
    """A few sagas with real-looking arguments, one of which aborts."""
    def charge(amount, customer_email):
        return {"id": "ch_1"}

    def ship(sku, note):
        return {"id": "s_1"}

    for _ in range(3):
        async with saga_scope(wal=wal, name="orders") as saga:
            await saga.execute(
                tool="stripe.charge", semantics=C, forward=charge,
                forward_kwargs={"amount": SECRET_AMOUNT,
                                "customer_email": SECRET_EMAIL},
                compensate=lambda r: Compensation(fn=lambda: None, description="x"))
            await saga.execute(
                tool="ship.order", semantics=C, forward=ship,
                forward_kwargs={"sku": "widget-9", "note": SECRET_NOTE},
                compensate=lambda r: Compensation(fn=lambda: None, description="x"))

    with pytest.raises(SagaAborted):
        async with saga_scope(wal=wal, name="orders") as saga:
            await saga.execute(
                tool="stripe.charge", semantics=C, forward=charge,
                forward_kwargs={"amount": SECRET_AMOUNT,
                                "customer_email": SECRET_EMAIL},
                compensate=lambda r: Compensation(fn=lambda: None, description="x"))

            def boom(**kw):
                raise ConnectionError("carrier down")

            await saga.execute(tool="ship.order", semantics=C, forward=boom,
                               forward_kwargs={"sku": "gizmo", "note": SECRET_NOTE},
                               compensate=lambda r: Compensation(
                                   fn=lambda: None, description="x"))


@pytest.fixture
async def real_records(tmp_path):
    wal = AsyncWAL(tmp_path / "real.wal")
    await wal.start()
    try:
        await real_traffic(wal)
        return await wal.read_all()
    finally:
        await wal.close()


# -- 1. no real value escapes -------------------------------------------------------

@aio
async def test_the_profile_itself_carries_no_real_values(tmp_path):
    """This is what makes the profile shareable: fit it where the data is, ship
    the profile, synthesise anywhere."""
    wal = AsyncWAL(tmp_path / "real.wal")
    await wal.start()
    try:
        await real_traffic(wal)
        records = await wal.read_all()
    finally:
        await wal.close()

    profile = WALProfile.fit(records)
    blob = json.dumps(profile.describe()) + profile.format_text()

    assert SECRET_EMAIL not in blob
    assert SECRET_NOTE not in blob
    assert str(SECRET_AMOUNT) not in blob
    assert "Contains no real values" in profile.format_text()


@aio
async def test_no_real_value_survives_into_the_synthetic_log(tmp_path):
    """The load-bearing privacy property. A leak here makes the whole feature a
    liability."""
    wal = AsyncWAL(tmp_path / "real.wal")
    await wal.start()
    try:
        await real_traffic(wal)
        records = await wal.read_all()
    finally:
        await wal.close()

    generated = synthesize(WALProfile.fit(records), sagas=50, seed=1)
    blob = json.dumps(generated)

    assert SECRET_EMAIL not in blob
    assert SECRET_NOTE not in blob
    assert str(SECRET_AMOUNT) not in blob
    # the shape is kept, though: the fields still exist
    intents = [r for r in generated if r["event"] == "STEP_INTENT"]
    charge = next(r for r in intents if r["tool"] == "stripe.charge")
    assert set(charge["kwargs"]) == {"amount", "customer_email"}


@aio
async def test_synthetic_strings_are_obviously_synthetic(tmp_path):
    """Not plausible fakes. A synthetic corpus that reads like real customer
    data invites exactly the confusion this module exists to avoid."""
    wal = AsyncWAL(tmp_path / "real.wal")
    await wal.start()
    try:
        await real_traffic(wal)
        records = await wal.read_all()
    finally:
        await wal.close()

    generated = synthesize(WALProfile.fit(records), sagas=5, seed=1)
    charge = next(r for r in generated
                  if r["event"] == "STEP_INTENT" and r["tool"] == "stripe.charge")
    assert charge["kwargs"]["customer_email"].startswith("synthetic_customer_email_")


def test_a_single_valued_numeric_field_is_not_reproduced_exactly():
    """The leak this module shipped with, now pinned.

    A field observed with one value gave `low == high`, and sampling from that
    interval returned the real number every time -- a certainty, not a chance.
    The stored range is widened so the degenerate case stops being a guaranteed
    disclosure.
    """
    records = [
        {"saga_id": "s1", "event": "SAGA_START"},
        {"saga_id": "s1", "event": "STEP_INTENT", "step_id": "a",
         "tool": "pay.send", "semantics": "COMPENSABLE",
         "kwargs": {"amount": 4242424}},
        {"saga_id": "s1", "event": "STEP_COMMITTED", "step_id": "a",
         "tool": "pay.send"},
        {"saga_id": "s1", "event": "SAGA_COMPLETE"},
    ]
    profile = WALProfile.fit(records)
    kind, low, high = profile.fields["pay.send\x1famount"]
    assert low < 4242424 < high, "the range was not widened"

    # across many draws the exact value must not dominate
    values = set()
    for seed in range(20):
        generated = synthesize(profile, sagas=1, seed=seed, chain=False)
        intent = next(r for r in generated if r["event"] == "STEP_INTENT")
        values.add(intent["kwargs"]["amount"])
    assert values != {4242424}


def test_redact_at_fit_time_drops_identifier_fields_entirely():
    """The honest answer for a numeric field that is an identifier rather than
    a magnitude: a range models an amount and mismodels an account number, and
    only the operator knows which is which."""
    records = [
        {"saga_id": "s1", "event": "SAGA_START"},
        {"saga_id": "s1", "event": "STEP_INTENT", "step_id": "a",
         "tool": "pay.send", "semantics": "COMPENSABLE",
         "kwargs": {"amount": 100, "account_number": 90210}},
        {"saga_id": "s1", "event": "STEP_COMMITTED", "step_id": "a",
         "tool": "pay.send"},
        {"saga_id": "s1", "event": "SAGA_COMPLETE"},
    ]
    profile = WALProfile.fit(records, redact=["account"])

    assert "pay.send\x1famount" in profile.fields
    assert "pay.send\x1faccount_number" not in profile.fields

    generated = synthesize(profile, sagas=5, seed=1, chain=False)
    blob = json.dumps(generated)
    assert "90210" not in blob
    assert "account_number" not in blob


# -- 2. everything is marked -----------------------------------------------------------

def test_every_generated_record_is_marked():
    profile = WALProfile(tools={"t": 1}, saga_lengths=[1])
    generated = synthesize(profile, sagas=3, seed=1)

    assert generated
    assert all(r[SYNTHETIC_FIELD] is True for r in generated)
    assert all(r["saga_id"].startswith("synthetic-") for r in generated)
    assert is_synthetic(generated)


def test_is_synthetic_is_strict_about_mixed_logs():
    """A log that is mostly synthetic is a log somebody mixed. Treating it as
    safe to share would be the exact mistake this guards against."""
    profile = WALProfile(tools={"t": 1}, saga_lengths=[1])
    generated = synthesize(profile, sagas=1, seed=1)

    assert is_synthetic(generated)
    assert not is_synthetic(generated + [{"event": "SAGA_START", "saga_id": "real-1"}])
    assert not is_synthetic([])                 # nothing proves nothing
    assert not is_synthetic({"event": "X"})


# -- 3. the output is structurally valid ------------------------------------------------

@aio
async def test_a_synthetic_log_passes_hash_chain_verification():
    """A fixture that fails integrity for the wrong reason teaches nothing."""
    from agent_saga.integrity import verify

    profile = WALProfile(tools={"stripe.charge": 5, "ship.order": 5},
                         saga_lengths=[2], abort_rate=0.5,
                         semantics={"stripe.charge": "COMPENSABLE",
                                    "ship.order": "COMPENSABLE"})
    generated = synthesize(profile, sagas=20, seed=3)

    report = verify(generated)
    assert report.intact, report.summary()


def test_event_ordering_is_valid_within_each_saga():
    profile = WALProfile(tools={"a.tool": 3, "b.tool": 2}, saga_lengths=[3],
                         abort_rate=1.0,
                         semantics={"a.tool": "COMPENSABLE", "b.tool": "COMPENSABLE"})
    generated = synthesize(profile, sagas=10, seed=5)

    by_saga = {}
    for record in generated:
        by_saga.setdefault(record["saga_id"], []).append(record)

    for saga_id, records in by_saga.items():
        events = [r["event"] for r in records]
        assert events[0] == "SAGA_START", saga_id
        assert events[-1] in ("SAGA_COMPLETE", "SAGA_ABORTED"), saga_id

        # every commit is preceded by its own intent
        seen_intents = set()
        for record in records:
            if record["event"] == "STEP_INTENT":
                seen_intents.add(record["step_id"])
            elif record["event"] in ("STEP_COMMITTED", "STEP_UNKNOWN"):
                assert record["step_id"] in seen_intents, saga_id

        # an abort rolls back exactly the steps that committed, LIFO
        if events[-1] == "SAGA_ABORTED":
            committed = [r["step_id"] for r in records
                         if r["event"] == "STEP_COMMITTED"]
            compensated = [r["step_id"] for r in records
                           if r["event"] == "COMPENSATED"]
            assert compensated == list(reversed(committed)), saga_id


def test_the_certifier_accepts_a_synthetic_log():
    from agent_saga.certify import certify_rollback_safety

    profile = WALProfile(tools={"stripe.charge": 4}, saga_lengths=[2],
                         abort_rate=1.0, semantics={"stripe.charge": "COMPENSABLE"})
    generated = synthesize(profile, sagas=15, seed=11)

    cert = certify_rollback_safety(generated)
    assert cert.safe, [f.issue for f in cert.findings]


def test_the_graph_exporter_and_corpus_builder_accept_it():
    from agent_saga.corpus import build_corpus
    from agent_saga.graph import wal_to_mermaid

    profile = WALProfile(tools={"stripe.charge": 3, "ship.order": 3},
                         saga_lengths=[2], abort_rate=0.5,
                         semantics={"stripe.charge": "COMPENSABLE",
                                    "ship.order": "COMPENSABLE"})
    generated = synthesize(profile, sagas=8, seed=2)

    assert wal_to_mermaid(generated).startswith("flowchart TD")
    corpus = build_corpus(generated)
    assert corpus.sagas == 8
    assert corpus.examples


# -- determinism and scale ----------------------------------------------------------------

def test_generation_is_deterministic_for_a_seed():
    """A fixture must be regenerable byte for byte, or a failing test stops
    staying failed."""
    profile = WALProfile(tools={"a": 2, "b": 1}, saga_lengths=[1, 2, 3],
                         abort_rate=0.4, unknown_rate=0.1)
    # chain=False: the hash stamp mixes a fresh random salt into every record
    # by design, so a stamped log differs run to run even from one seed. The
    # CONTENT is what a fixture needs to be reproducible.
    first = synthesize(profile, sagas=25, seed=42, chain=False)
    second = synthesize(profile, sagas=25, seed=42, chain=False)
    third = synthesize(profile, sagas=25, seed=43, chain=False)

    assert first == second
    assert first != third


def test_a_stamped_log_is_reproducible_in_content_but_not_in_salt():
    """Stated rather than glossed: `chain=True` deliberately costs byte
    determinism, because the salt is what stops a redacted field being
    dictionary-attacked."""
    profile = WALProfile(tools={"a": 1}, saga_lengths=[2])
    first = synthesize(profile, sagas=5, seed=1)
    second = synthesize(profile, sagas=5, seed=1)

    assert first != second                      # salts differ
    strip = lambda rs: [{k: v for k, v in r.items() if not k.startswith("_")}
                        for r in rs]
    assert strip(first) == strip(second)         # content identical


def test_it_scales_past_what_anyone_has_real_failures_for():
    profile = WALProfile(tools={"a": 1}, saga_lengths=[2], abort_rate=1.0)
    generated = synthesize(profile, sagas=2_000, seed=1)
    sagas = {r["saga_id"] for r in generated}
    assert len(sagas) == 2_000


# -- fitting ---------------------------------------------------------------------------------

@aio
async def test_fitting_learns_tools_lengths_and_rates(tmp_path):
    wal = AsyncWAL(tmp_path / "real.wal")
    await wal.start()
    try:
        await real_traffic(wal)
        records = await wal.read_all()
    finally:
        await wal.close()

    profile = WALProfile.fit(records)
    assert set(profile.tools) == {"stripe.charge", "ship.order"}
    assert profile.semantics["stripe.charge"] == "COMPENSABLE"
    assert profile.mean_length == pytest.approx(2.0)
    assert 0 < profile.abort_rate <= 1.0            # one of four sagas aborted
    assert "stripe.charge" in profile.transitions   # charge is followed by ship


def test_an_empty_profile_refuses_to_generate():
    """Vacuous output would look like a working fixture and exercise nothing."""
    with pytest.raises(ValueError, match="nothing to generate"):
        synthesize(WALProfile(), sagas=5)
    assert "generates nothing" in WALProfile().format_text()


def test_a_nonsense_saga_count_is_refused():
    with pytest.raises(ValueError, match="must be >= 1"):
        synthesize(WALProfile(tools={"t": 1}, saga_lengths=[1]), sagas=0)


# -- the CTGAN hook --------------------------------------------------------------------------

def test_a_custom_value_synthesizer_is_used():
    """Where a CTGAN sampler plugs in. The default is deliberately simple; this
    is the seam for something learned."""
    calls = []

    def fake_ctgan(tool, field, kind, low, high):
        calls.append((tool, field, kind))
        return f"<{kind}:from-model>"

    profile = WALProfile(tools={"t.x": 1}, saga_lengths=[1],
                         fields={"t.x\x1famount": ("number", 1.0, 10.0)})
    generated = synthesize(profile, sagas=1, seed=1, value_synthesizer=fake_ctgan)

    intent = next(r for r in generated if r["event"] == "STEP_INTENT")
    assert intent["kwargs"]["amount"] == "<number:from-model>"
    assert calls == [("t.x", "amount", "number")]
