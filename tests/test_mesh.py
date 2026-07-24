"""Offline-first WAL sync (Phase 2.1): deterministic, conflict-free merge.

The CRDT laws are the whole contract -- if any of them fails, two devices can
look at the same marketplace and disagree about what happened.
"""

import itertools

from conftest import aio
from agent_saga.mesh import (
    DEVICE_FIELD, merge_wals, record_identity, split_by_device, verify_merged)


def _ids(records):
    return [record_identity(r) for r in records]


SHARED = {"seq": 1, "saga_id": "sync-1", "event": "SAGA_START", "ts": 1.0}
A = [SHARED,
     {"seq": 2, "saga_id": "saga-A", "event": "SAGA_START", "ts": 5.0},
     {"seq": 3, "saga_id": "saga-A", "event": "STEP_COMMITTED", "tool": "swap", "ts": 6.0}]
B = [SHARED,
     {"seq": 2, "saga_id": "saga-B", "event": "SAGA_START", "ts": 4.0},
     {"seq": 3, "saga_id": "saga-B", "event": "STEP_COMMITTED", "tool": "rent", "ts": 7.0}]
C = [{"seq": 9, "saga_id": "saga-C", "event": "SAGA_START", "ts": 2.0}]


# -- the CRDT laws ------------------------------------------------------------

def test_commutative():
    ab, _ = merge_wals({"a": A, "b": B})
    ba, _ = merge_wals({"b": B, "a": A})
    assert _ids(ab) == _ids(ba)


def test_idempotent():
    ab, _ = merge_wals({"a": A, "b": B})
    again, _ = merge_wals({"m": ab, "a": A, "b": B})
    assert _ids(again) == _ids(ab)


def test_associative():
    left, _ = merge_wals({"m": merge_wals({"a": A, "b": B})[0], "c": C})
    right, _ = merge_wals({"a": A, "m": merge_wals({"b": B, "c": C})[0]})
    assert _ids(left) == _ids(right)


def test_order_independent_across_every_permutation():
    """No matter which peer syncs first, every device computes the same log."""
    baseline = None
    for perm in itertools.permutations([("a", A), ("b", B), ("c", C)]):
        merged, _ = merge_wals(dict(perm))
        if baseline is None:
            baseline = _ids(merged)
        assert _ids(merged) == baseline


# -- identity and dedupe ------------------------------------------------------

def test_shared_record_dedupes():
    merged, report = merge_wals({"a": A, "b": B})
    assert sum(1 for r in merged if r["saga_id"] == "sync-1") == 1
    assert report.duplicates == 1


def test_same_seq_on_different_devices_is_not_a_collision():
    # both devices have seq=2 for different sagas; identity is content-derived
    merged, _ = merge_wals({"a": A, "b": B})
    seq2 = [r for r in merged if r.get("seq") == 2]
    assert len(seq2) == 2
    assert {r["saga_id"] for r in seq2} == {"saga-A", "saga-B"}


def test_identity_ignores_merge_metadata():
    rec = dict(SHARED)
    stamped = dict(SHARED, **{DEVICE_FIELD: "phone-a"})
    assert record_identity(rec) == record_identity(stamped)


def test_origin_is_preserved_through_a_third_peer():
    once, _ = merge_wals({"phone-a": A})
    # relaying A's records through a different peer must not re-attribute them
    relayed, _ = merge_wals({"phone-z": once})
    assert all(r[DEVICE_FIELD] == "phone-a" for r in relayed)


# -- divergence ---------------------------------------------------------------

def test_record_synced_to_both_devices_is_not_divergence():
    _, report = merge_wals({"a": A, "b": B})
    assert report.diverged_sagas == []      # SHARED is one record, not a conflict


def test_saga_advanced_on_two_devices_is_flagged():
    d1 = [{"seq": 1, "saga_id": "job-7", "event": "SAGA_START", "ts": 1.0}]
    d2 = [{"seq": 1, "saga_id": "job-7", "event": "STEP_COMMITTED", "tool": "x", "ts": 2.0}]
    _, report = merge_wals({"a": d1, "b": d2})
    assert report.diverged_sagas == ["job-7"]


# -- chains -------------------------------------------------------------------

@aio
async def test_per_device_chains_verify_and_localise_tampering(tmp_path):
    from agent_saga.wal.file_wal import FileWAL

    async def device(name, n):
        w = FileWAL(tmp_path / f"{name}.wal")
        await w.start()
        for i in range(n):
            w.append("SAGA_START", {"saga_id": f"{name}-{i}"})
            await w.barrier()
        await w.close()
        return w.records()

    a = await device("phone-a", 3)
    b = await device("phone-b", 2)
    merged, _ = merge_wals({"phone-a": a, "phone-b": b})

    result = verify_merged(merged)
    assert result["intact"]
    assert result["devices"]["phone-a"]["records"] == 3

    # tamper with one device's history -> only that device fails
    merged[0]["saga_id"] = "TAMPERED"
    bad = verify_merged(merged)
    assert not bad["intact"]
    failed = [d for d, i in bad["devices"].items() if not i["intact"]]
    assert len(failed) == 1


def test_split_by_device_round_trips():
    merged, _ = merge_wals({"a": A, "b": B})
    groups = split_by_device(merged)
    assert set(groups) == {"a", "b"}
    assert sum(len(v) for v in groups.values()) == len(merged)


def test_accepts_a_plain_sequence_of_logs():
    merged, report = merge_wals([A, B])
    assert report.devices == ["device-0", "device-1"]
    assert len(merged) == 5


def test_empty_and_single_source():
    merged, report = merge_wals({})
    assert merged == [] and report.total == 0
    merged, _ = merge_wals({"a": A})
    assert len(merged) == 3


# -- CLI ----------------------------------------------------------------------

def test_cli_merge(tmp_path, capsys):
    import json
    from agent_saga.cli import main

    for name, recs in (("phone-a", A), ("phone-b", B)):
        (tmp_path / f"{name}.wal").write_text(
            "\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")

    out = tmp_path / "merged.wal"
    rc = main(["merge",
               "--wal", f"phone-a={tmp_path / 'phone-a.wal'}",
               "--wal", f"phone-b={tmp_path / 'phone-b.wal'}",
               "--out", str(out)])
    assert rc == 0
    assert "merged 5 record(s) from 2 device(s)" in capsys.readouterr().out
    lines = [l for l in out.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 5
