"""Adversarial property tests for the mesh CRDT merge.

The merge runs on records that arrived from another device. Nothing about them
is trustworthy: a peer can send a malformed record, a hostile clock, a colliding
sequence number, or a device id chosen to sort in front of everyone else's.

The laws still have to hold, because they are the whole contract -- if two
devices compute different logs from the same facts, the log stops being
evidence. These tests generate hostile inputs from fixed seeds (so a failure is
reproducible) and assert the laws survive.

This suite found the NaN-timestamp bug: `sorted()` compares every NaN as False,
so one record carrying `"ts": NaN` made the merge order depend on input order,
breaking commutativity in 138 of 400 cases.
"""

import math
import random

import pytest

from agent_saga.mesh import DEVICE_FIELD, merge_wals, record_identity, split_by_device

# Values a hostile or buggy peer can plausibly put in a record.
HOSTILE_TS = [0, -1, 1e18, -1e18, float("nan"), float("inf"), float("-inf"),
              True, False, None, "abc", ""]
HOSTILE_SEQ = [0, -5, 2 ** 63, -(2 ** 63), None, "x", True]
HOSTILE_SAGA = [None, "", "   ", "unicode-é中" * 5, "a b", 123, ("tuple",)]
HOSTILE_DEVICE = ["", " ", "!!!", "é", "a" * 200, "0", "zzz", "device-0"]


def _record(rng):
    r = {"event": rng.choice(["SAGA_START", "STEP_COMMITTED", "ROLLBACK_END", None, ""])}
    if rng.random() < 0.9:
        r["ts"] = rng.choice(HOSTILE_TS)
    if rng.random() < 0.9:
        r["seq"] = rng.choice(HOSTILE_SEQ)
    if rng.random() < 0.9:
        r["saga_id"] = rng.choice(HOSTILE_SAGA)
    if rng.random() < 0.3:
        r["nested"] = {"a": [1, {"b": None}], "c": rng.choice(HOSTILE_TS)}
    if rng.random() < 0.15:
        r[DEVICE_FIELD] = rng.choice(HOSTILE_DEVICE)     # peer claims an origin
    return r


def _logs(seed, n=3):
    rng = random.Random(seed)
    return [[_record(rng) for _ in range(rng.randint(0, 7))] for _ in range(n)]


def _ids(records):
    return [record_identity(r) for r in records]


SEEDS = list(range(200))


@pytest.mark.parametrize("seed", SEEDS)
def test_commutative_under_hostile_input(seed):
    a, b, _ = _logs(seed)
    ab, _ = merge_wals({"alpha": a, "beta": b})
    ba, _ = merge_wals({"beta": b, "alpha": a})
    assert _ids(ab) == _ids(ba)


@pytest.mark.parametrize("seed", SEEDS)
def test_associative_under_hostile_input(seed):
    a, b, c = _logs(seed)
    left, _ = merge_wals({"m": merge_wals({"alpha": a, "beta": b})[0], "gamma": c})
    right, _ = merge_wals({"alpha": a, "m": merge_wals({"beta": b, "gamma": c})[0]})
    assert _ids(left) == _ids(right)


@pytest.mark.parametrize("seed", SEEDS)
def test_idempotent_under_hostile_input(seed):
    a, b, _ = _logs(seed)
    once, _ = merge_wals({"alpha": a, "beta": b})
    twice, _ = merge_wals({"m": once, "alpha": a, "beta": b})
    assert _ids(twice) == _ids(once)


@pytest.mark.parametrize("seed", SEEDS[:60])
def test_merge_never_raises_on_hostile_input(seed):
    a, b, c = _logs(seed)
    merged, report = merge_wals({"alpha": a, "beta": b, "gamma": c})
    assert report.total == len(merged)
    # every surviving record is uniquely identified
    assert len(set(_ids(merged))) == len(merged)
    # and is attributable to exactly one device
    assert sum(len(v) for v in split_by_device(merged).values()) == len(merged)


# -- the specific defect this suite found -------------------------------------

def test_nan_timestamp_cannot_break_the_total_order():
    """A single NaN timestamp once made the merge order input-dependent, so two
    devices disagreed about their shared history."""
    nan_rec = {"seq": 1, "saga_id": "s", "event": "SAGA_START", "ts": float("nan")}
    others = [{"seq": i, "saga_id": "s", "event": "X", "ts": float(i)} for i in range(6)]
    a = [nan_rec] + others[:3]
    b = others[3:]

    ab, _ = merge_wals({"alpha": a, "beta": b})
    ba, _ = merge_wals({"beta": b, "alpha": a})
    assert _ids(ab) == _ids(ba)
    # the NaN record survives -- it is normalised for ordering, not dropped
    assert any(isinstance(r.get("ts"), float) and math.isnan(r["ts"]) for r in ab)


def test_infinite_timestamps_stay_deterministic():
    a = [{"seq": 1, "saga_id": "s", "ts": float("inf")},
         {"seq": 2, "saga_id": "s", "ts": 5.0}]
    b = [{"seq": 3, "saga_id": "s", "ts": float("-inf")}]
    ab, _ = merge_wals({"alpha": a, "beta": b})
    ba, _ = merge_wals({"beta": b, "alpha": a})
    assert _ids(ab) == _ids(ba)
    # -inf sorts first, +inf last: a peer can nudge position but not break agreement
    assert ab[0]["ts"] == float("-inf") and ab[-1]["ts"] == float("inf")


def test_bool_timestamps_do_not_alias_integers_unpredictably():
    # True/False are ints in Python; they must order deterministically either way
    a = [{"seq": 1, "saga_id": "s", "ts": True}]
    b = [{"seq": 2, "saga_id": "s", "ts": 1.0}]
    ab, _ = merge_wals({"alpha": a, "beta": b})
    ba, _ = merge_wals({"beta": b, "alpha": a})
    assert _ids(ab) == _ids(ba)


def test_hostile_device_id_cannot_reattribute_another_peers_record():
    """A peer claiming `_dev: "aaa"` cannot steal a record that another device
    also holds: the smallest origin wins deterministically, both ways round."""
    shared = {"seq": 1, "saga_id": "s", "event": "SAGA_START", "ts": 1.0}
    honest = [dict(shared)]
    liar = [dict(shared, **{DEVICE_FIELD: "aaa"})]

    ab, _ = merge_wals({"honest": honest, "liar": liar})
    ba, _ = merge_wals({"liar": liar, "honest": honest})
    assert _ids(ab) == _ids(ba)
    assert len(ab) == 1                       # still one record, not two
    assert ab[0][DEVICE_FIELD] == ba[0][DEVICE_FIELD]   # and one agreed origin


def test_colliding_seq_across_devices_never_merges_distinct_records():
    a = [{"seq": 1, "saga_id": "A", "event": "X", "ts": 1.0}]
    b = [{"seq": 1, "saga_id": "B", "event": "X", "ts": 1.0}]
    merged, _ = merge_wals({"alpha": a, "beta": b})
    assert len(merged) == 2                   # same seq, different content
