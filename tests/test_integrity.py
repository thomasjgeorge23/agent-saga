"""Tamper-evident WAL: the hash chain, redaction, attested gaps, WORM export.

The tests that matter are the adversarial ones. A chain that verifies a healthy
log proves nothing; what has to hold is that every way of editing the log is
caught, and that the two *legitimate* mutations -- compaction and GDPR erasure --
are still accepted.
"""

import copy
import json
import tempfile
from pathlib import Path

import pytest

from conftest import aio

from agent_saga.integrity import (
    GAP_EVENT,
    GENESIS,
    HASH_FIELD,
    PREV_FIELD,
    REDACTED_FIELD,
    SALT_FIELD,
    as_runs,
    export_worm,
    gap_attestation,
    redact_record,
    redact_where,
    stamp_batch,
    verify,
)
from agent_saga.wal import FileWAL


def chained(n=6, event="STEP_COMMITTED"):
    records = [{"seq": i + 1, "ts": 1000.0 + i, "event": event,
                "saga_id": f"s{i}", "tool": "stripe.charge", "amount": 100 * i}
               for i in range(n)]
    stamp_batch(records, GENESIS)
    return records


# ---------------------------------------------------------------------------
# 1. A healthy chain
# ---------------------------------------------------------------------------

def test_a_clean_chain_verifies():
    report = verify(chained())
    assert report.intact
    assert report.checked == 6
    assert report.head != GENESIS


def test_every_record_carries_its_own_link():
    records = chained(3)
    assert records[0][PREV_FIELD] == GENESIS
    for earlier, later in zip(records, records[1:]):
        assert later[PREV_FIELD] == earlier[HASH_FIELD]


# ---------------------------------------------------------------------------
# 2. Tampering -- the whole point
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,mutate", [
    ("alter an amount",
     lambda r: r[2].update(amount=1)),
    ("alter the tool name",
     lambda r: r[2].update(tool="wire.send")),
    ("alter a timestamp",
     lambda r: r[2].update(ts=0.0)),
    ("alter the event type",
     lambda r: r[2].update(event="SAGA_COMPLETE")),
    ("delete a record",
     lambda r: r.pop(2)),
    ("reorder two records",
     lambda r: r.__setitem__(slice(1, 3), [r[2], r[1]])),
    ("truncate the head",
     lambda r: r.pop(0)),
    ("append a forged record",
     lambda r: r.append(dict(r[-1], seq=99, saga_id="ghost", amount=50_000))),
    ("splice a forged record into the middle",
     lambda r: r.insert(3, dict(r[3], saga_id="ghost", amount=50_000))),
    ("rewrite a chain pointer",
     lambda r: r[3].update({PREV_FIELD: r[1][HASH_FIELD]})),
    ("strip the salt to hide an edit",
     lambda r: (r[2].pop(SALT_FIELD), r[2].update(amount=1))),
])
def test_tampering_is_detected(name, mutate):
    records = chained()
    mutate(records)
    report = verify(records)
    assert not report.intact, f"{name} went undetected"
    assert report.breaks[0].reason


def test_a_forged_record_cannot_be_rechained_without_the_tail():
    """An attacker who edits a record and recomputes its hash still breaks the
    link to the record after it, because that one commits to the old hash."""
    records = chained()
    victim = records[2]
    victim["amount"] = 1
    stamp_batch([victim], records[1][HASH_FIELD])   # re-stamp convincingly

    report = verify(records)
    assert not report.intact
    assert "link" in report.breaks[0].reason


def test_one_break_does_not_cascade_into_many():
    """Verification continues from what the log claims, so an operator sees the
    one record that was touched rather than every record after it."""
    records = chained(10)
    records[4]["amount"] = 1
    report = verify(records)
    assert len(report.breaks) == 1
    assert report.breaks[0].seq == 5


# ---------------------------------------------------------------------------
# 3. Redaction -- GDPR erasure that keeps the chain provable
# ---------------------------------------------------------------------------

def test_redaction_erases_the_payload_and_keeps_the_chain():
    records = chained()
    records, count = redact_where(records, lambda r: r.get("saga_id") == "s3")
    assert count == 1

    report = verify(records)
    assert report.intact, "redaction must not break the chain"
    assert report.redacted == 1

    erased = next(r for r in records if r.get(REDACTED_FIELD))
    assert "amount" not in erased and "saga_id" not in erased
    assert SALT_FIELD not in erased, "the salt must be destroyed, not merely hidden"
    # What survives is proof of existence, position, and type.
    assert erased["seq"] == 4 and erased["event"] == "STEP_COMMITTED"


def test_a_redacted_record_cannot_be_brute_forced():
    """The salt is what stops {'amount': 4200} being recovered from its own
    digest by trying every plausible amount."""
    from agent_saga.integrity import DIGEST_FIELD, content_digest

    records = chained(2)
    erased = redact_record(records[1])
    guesses = [{"saga_id": "s1", "tool": "stripe.charge", "amount": a}
               for a in range(0, 1000)]
    assert all(content_digest(g, "") != erased[DIGEST_FIELD] for g in guesses)


def test_redaction_is_visible_not_silent():
    records = chained(3)
    records, _ = redact_where(records, lambda r: r.get("seq") == 2,
                              reason="gdpr-art-17")
    assert records[1][REDACTED_FIELD] == "gdpr-art-17"
    assert verify(records).redacted == 1


def test_an_unchained_record_cannot_be_redacted():
    with pytest.raises(ValueError):
        redact_record({"seq": 1, "event": "X", "saga_id": "s"})


def test_redact_path_with_list_index():
    from agent_saga.integrity import redact_path
    records = [
        {"seq": 1, "kwargs": {"items": [{"card": "1234-5678", "cvv": "999"}]}}
    ]
    out, count = redact_path(records, "kwargs.items.0.cvv")
    assert count == 1
    assert out[0]["kwargs"]["items"][0]["cvv"] == "[REDACTED]"
    assert out[0]["kwargs"]["items"][0]["card"] == "1234-5678"


def test_editing_a_redacted_record_is_still_caught():
    records = chained(4)
    records, _ = redact_where(records, lambda r: r.get("seq") == 2)
    records[1]["event"] = "SAGA_COMPLETE"
    assert not verify(records).intact


# ---------------------------------------------------------------------------
# 4. Attested gaps -- compaction is legitimate, deletion is not
# ---------------------------------------------------------------------------

def test_runs_compress_scattered_sequences():
    assert as_runs([1, 2, 3, 7, 8, 20]) == [[1, 3], [7, 8], [20, 20]]
    assert as_runs([]) == []


def test_an_attested_gap_is_accepted():
    records = chained(6)
    survivors = [r for r in records if r["seq"] in (5, 6)]
    attestation = gap_attestation([1, 2, 3, 4], "digest", "compaction")
    attestation.update(seq=7, ts=2000.0)
    stamp_batch([attestation], survivors[-1][HASH_FIELD])
    survivors.append(attestation)

    report = verify(survivors)
    assert report.intact
    assert report.attested_gaps == 1


def test_an_unattested_gap_is_rejected():
    records = chained(6)
    del records[2]
    report = verify(records)
    assert not report.intact
    assert GAP_EVENT in report.breaks[0].reason


def test_an_attestation_cannot_cover_a_record_it_does_not_name():
    records = chained(6)
    survivors = [r for r in records if r["seq"] in (5, 6)]
    attestation = gap_attestation([1, 2], "digest", "partial")   # omits 3, 4
    attestation.update(seq=7, ts=2000.0)
    stamp_batch([attestation], survivors[-1][HASH_FIELD])
    survivors.append(attestation)

    report = verify(survivors)
    assert not report.intact
    assert "3" in report.breaks[0].reason


# ---------------------------------------------------------------------------
# 5. Live WAL integration
# ---------------------------------------------------------------------------

@aio
async def test_a_live_wal_writes_a_verifiable_chain():
    with tempfile.TemporaryDirectory() as d:
        wal = FileWAL(Path(d) / "w.jsonl")
        await wal.start()
        for i in range(5):
            wal.append("STEP_COMMITTED", {"saga_id": f"s{i}", "amount": i})
        await wal.barrier()
        report = verify(wal.records())
        await wal.close()
    assert report.intact and report.checked == 5


@aio
async def test_a_restart_continues_one_chain():
    """A restart that began a fresh chain would leave a seam that proves nothing
    about what came before it -- exactly where a record would be inserted."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "w.jsonl"
        wal = FileWAL(path)
        await wal.start()
        wal.append("SAGA_START", {"saga_id": "a"})
        await wal.barrier()
        first_head = wal._chain_head
        await wal.close()

        again = FileWAL(path)
        await again.start()
        assert again._chain_head == first_head, "chain head not resumed"
        again.append("SAGA_COMPLETE", {"saga_id": "a"})
        await again.barrier()
        records = again.records()
        await again.close()

    assert verify(records).intact
    assert records[1][PREV_FIELD] == records[0][HASH_FIELD]


@aio
async def test_compaction_attests_what_it_removed():
    with tempfile.TemporaryDirectory() as d:
        wal = FileWAL(Path(d) / "w.jsonl")
        await wal.start()
        for i in range(6):
            wal.append("SAGA_COMPLETE", {"saga_id": f"s{i}"})
        await wal.barrier()
        await wal.compact(keep_saga_ids={"s4", "s5"})
        await wal.barrier()
        records = wal.records()
        await wal.close()

    report = verify(records)
    assert report.intact, report.summary()
    assert report.attested_gaps == 1

    # Deleting a survivor is still tampering.
    assert not verify([r for r in records if r.get("saga_id") != "s4"]).intact
    # And so is deleting the attestation that explains the gap.
    assert not verify([r for r in records if r.get("event") != GAP_EVENT]).intact


@aio
async def test_attestations_survive_a_later_compaction():
    """If housekeeping dropped the attestation, the gap it explained would start
    reading as an attack."""
    with tempfile.TemporaryDirectory() as d:
        wal = FileWAL(Path(d) / "w.jsonl")
        await wal.start()
        for i in range(6):
            wal.append("SAGA_COMPLETE", {"saga_id": f"s{i}"})
        await wal.barrier()
        await wal.compact(keep_saga_ids={"s4", "s5"})
        await wal.barrier()
        await wal.compact(keep_saga_ids={"s5"})
        await wal.barrier()
        records = wal.records()
        await wal.close()

    assert sum(1 for r in records if r.get("event") == GAP_EVENT) == 2
    assert verify(records).intact


@aio
async def test_chaining_can_be_turned_off():
    with tempfile.TemporaryDirectory() as d:
        wal = FileWAL(Path(d) / "w.jsonl", chain=False)
        await wal.start()
        wal.append("SAGA_START", {"saga_id": "a"})
        await wal.barrier()
        records = wal.records()
        await wal.close()

    assert HASH_FIELD not in records[0]
    assert verify(records).intact            # tolerated by default
    assert not verify(records, strict=True).intact


# ---------------------------------------------------------------------------
# 6. WORM export
# ---------------------------------------------------------------------------

def test_export_writes_a_self_describing_bundle():
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "bundle"
        manifest = export_worm(chained(4), str(out), source="w.jsonl")

        assert (out / "records.jsonl").exists()
        assert (out / "manifest.json").exists()
        assert manifest["intact"] and manifest["records"] == 4
        # The rule must be written down, so the bundle outlives this library.
        assert "sha256" in manifest["verification"]["record_hash"]

        written = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        assert written["bundle_sha256"] == manifest["bundle_sha256"]


def test_an_exported_bundle_re_verifies_from_disk():
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "bundle"
        export_worm(chained(5), str(out))
        reread = [json.loads(line) for line
                  in (out / "records.jsonl").read_text(encoding="utf-8").splitlines()]
    assert verify(reread).intact


def test_the_bundle_digest_covers_the_bytes_on_disk():
    import hashlib

    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "bundle"
        manifest = export_worm(chained(3), str(out))
        raw = (out / "records.jsonl").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == manifest["bundle_sha256"]


def test_a_broken_chain_is_exported_labelled_broken():
    records = chained(4)
    records[1]["amount"] = 999
    with tempfile.TemporaryDirectory() as d:
        manifest = export_worm(records, str(Path(d) / "bundle"))
    assert manifest["intact"] is False
    assert manifest["breaks"], "a broken export must say why"


# ---------------------------------------------------------------------------
# 7. CLI
# ---------------------------------------------------------------------------

def _write(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def test_cli_verify_exit_codes(capsys):
    from agent_saga.cli import main

    with tempfile.TemporaryDirectory() as d:
        good = Path(d) / "good.wal"
        _write(good, chained(4))
        assert main(["verify", "--wal-path", str(good)]) == 0
        assert "intact" in capsys.readouterr().out

        bad_records = chained(4)
        bad_records[2]["amount"] = 1
        bad = Path(d) / "bad.wal"
        _write(bad, bad_records)
        assert main(["verify", "--wal-path", str(bad)]) == 1
        assert "BROKEN" in capsys.readouterr().out

        assert main(["verify", "--wal-path", str(Path(d) / "nope.wal")]) == 2


def test_cli_export_refuses_a_broken_chain_by_default(capsys):
    from agent_saga.cli import main

    records = chained(4)
    records[2]["amount"] = 1
    with tempfile.TemporaryDirectory() as d:
        wal = Path(d) / "bad.wal"
        _write(wal, records)
        out = Path(d) / "bundle"

        assert main(["export", "--wal-path", str(wal), "--out", str(out)]) == 1
        # Diagnostics go to stderr so stdout stays clean for piped exports.
        assert "refusing to export" in capsys.readouterr().err
        assert not out.exists(), "a refused export must not leave an artifact"

        assert main(["export", "--wal-path", str(wal), "--out", str(out),
                     "--allow-broken"]) == 1
        assert (out / "manifest.json").exists()


def test_cli_export_round_trips_through_verify(capsys):
    from agent_saga.cli import main

    with tempfile.TemporaryDirectory() as d:
        wal = Path(d) / "w.wal"
        _write(wal, chained(5))
        out = Path(d) / "bundle"
        assert main(["export", "--wal-path", str(wal), "--out", str(out)]) == 0
        capsys.readouterr()
        assert main(["verify", "--wal-path", str(out / "records.jsonl")]) == 0
