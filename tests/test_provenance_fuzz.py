"""Adversarial tests for the Merkle selective-disclosure implementation.

The verifier runs on a file supplied by the party being audited. Two properties
have to hold against anything they can send:

  * it must never return valid=True for something it cannot actually prove
  * it must never raise -- a crash lets a discloser turn a failed proof into
    what looks like a broken tool

This suite found 139 crashes on malformed bundles (AttributeError, KeyError,
TypeError) and one real weakening: omitting `saga_id` silently skipped the
saga-scope check, so a bundle could carry records belonging to other sagas.
"""

import copy
import random

import pytest

from agent_saga.provenance import (
    ALGORITHM, MerkleAuditTree, build_disclosure, leaf_hash, verify_disclosure,
    verify_inclusion)

HOSTILE = [0, -1, 1e18, float("nan"), float("inf"), True, None, "", "  ",
           "unicode-é中", 2 ** 63, [1, {"a": None}], {"k": [None]}]

JUNK = [None, "", "zz", 0, -1, [], {}, True, "g" * 64, "0" * 63, "0" * 65,
        float("nan"), [["nonhex", "L"]], [[None, None]], [["aa", "X"]],
        "not-a-hash", {"a": 1}, [1, 2, 3]]


def _log(rng, n):
    out = []
    for i in range(n):
        r = {"seq": i, "saga_id": rng.choice(["A", "B", "C"]), "event": "X"}
        if rng.random() < 0.7:
            r["ts"] = rng.choice(HOSTILE)
        if rng.random() < 0.5:
            r["payload"] = rng.choice(HOSTILE)
        out.append(r)
    return out


# -- tree correctness across every shape --------------------------------------

@pytest.mark.parametrize("n", list(range(1, 34)))
def test_every_leaf_provable_at_every_tree_size(n):
    """Odd sizes exercise the self-pairing padding, where an off-by-one hides."""
    records = [{"seq": i, "saga_id": "A", "event": "X"} for i in range(n)]
    tree = MerkleAuditTree(records)
    for i in range(n):
        assert verify_inclusion(tree.leaves[i], tree.inclusion_proof(i), tree.root)


def test_identical_records_remain_individually_provable():
    dup = [{"seq": 1, "saga_id": "A", "event": "X"}] * 5
    tree = MerkleAuditTree(dup)
    assert all(verify_inclusion(tree.leaves[i], tree.inclusion_proof(i), tree.root)
               for i in range(5))


def test_large_tree():
    records = [{"seq": i, "saga_id": "A", "event": "X"} for i in range(1000)]
    tree = MerkleAuditTree(records)
    assert all(verify_inclusion(tree.leaves[i], tree.inclusion_proof(i), tree.root)
               for i in range(0, 1000, 97))


# -- forgery resistance under fuzz --------------------------------------------

@pytest.mark.parametrize("seed", list(range(60)))
def test_hostile_logs_resist_forgery(seed):
    rng = random.Random(seed)
    records = _log(rng, rng.randint(1, 20))
    tree = MerkleAuditTree(records)
    root = tree.root
    target = records[0]["saga_id"]
    bundle = build_disclosure(records, target)
    if not bundle["entries"]:
        return

    # a record never in the log cannot borrow a valid path
    alien = {"seq": 9999, "saga_id": target, "event": "FORGED"}
    assert not verify_inclusion(leaf_hash(alien), tree.inclusion_proof(0), root)

    # tampering with a disclosed record
    t = copy.deepcopy(bundle)
    t["entries"][0]["record"]["event"] = "TAMPERED"
    assert not verify_disclosure(t, expected_root=root).valid

    # appending a fabricated entry
    f = copy.deepcopy(bundle)
    fake = {"seq": 1234, "saga_id": target, "event": "FABRICATED"}
    f["entries"].append({"index": 1234, "leaf": leaf_hash(fake), "record": fake,
                         "path": f["entries"][0]["path"]})
    assert not verify_disclosure(f, expected_root=root).valid

    # truncating a proof path
    if len(bundle["entries"][0]["path"]) > 1:
        tr = copy.deepcopy(bundle)
        tr["entries"][0]["path"] = tr["entries"][0]["path"][:-1]
        assert not verify_disclosure(tr, expected_root=root).valid


def test_internal_node_cannot_pose_as_a_leaf():
    """The classic second-preimage attack, blocked by domain separation."""
    records = [{"seq": i, "saga_id": "A", "event": "X"} for i in range(8)]
    tree = MerkleAuditTree(records)
    internal = tree._levels[1][0]
    bundle = build_disclosure(records, "A")
    bundle["entries"] = [{"index": 0, "leaf": internal,
                          "record": {"seq": 0, "saga_id": "A"},
                          "path": tree.inclusion_proof(0)[1:]}]
    assert not verify_disclosure(bundle, expected_root=tree.root).valid


# -- the verifier must be total ------------------------------------------------

@pytest.mark.parametrize("seed", list(range(200)))
def test_malformed_bundles_never_crash(seed):
    rng = random.Random(seed)
    records = _log(rng, rng.randint(1, 10))
    tree = MerkleAuditTree(records)
    bundle = build_disclosure(records, "A")
    b = copy.deepcopy(bundle)

    what = rng.choice(["root", "alg", "entries", "entry_field", "path",
                       "drop_key", "entries_type", "record_type"])
    if what == "root":
        b["merkle_root"] = rng.choice(JUNK)
    elif what == "alg":
        b["algorithm"] = rng.choice(JUNK)
    elif what == "entries":
        b["entries"] = rng.choice(JUNK)
    elif what == "entries_type" and b.get("entries"):
        b["entries"] = [rng.choice(JUNK) for _ in b["entries"]]
    elif what == "entry_field" and b.get("entries"):
        e = rng.choice(b["entries"])
        e[rng.choice(["index", "leaf", "record", "path"])] = rng.choice(JUNK)
    elif what == "path" and b.get("entries"):
        rng.choice(b["entries"])["path"] = rng.choice(JUNK)
    elif what == "record_type" and b.get("entries"):
        rng.choice(b["entries"])["record"] = rng.choice(JUNK)
    elif what == "drop_key":
        b.pop(rng.choice(list(b.keys())), None)

    result = verify_disclosure(b, expected_root=tree.root)   # must not raise
    assert isinstance(result.valid, bool)


@pytest.mark.parametrize("junk", JUNK)
def test_verify_inclusion_is_total(junk):
    # A multi-leaf tree, so no path (including an empty one) can reach the root
    # by accident. In a single-leaf tree the root *is* the leaf and the empty
    # path is a legitimate proof -- which is correct, not a hole.
    tree = MerkleAuditTree([{"seq": i, "saga_id": "A"} for i in range(4)])
    assert verify_inclusion(junk, junk, junk) is False
    assert verify_inclusion(tree.leaves[0], junk, tree.root) is False
    assert verify_inclusion(junk, [], tree.root) is False


def test_single_leaf_tree_root_is_the_leaf():
    """Documents the edge case above: with one record the root is that record's
    leaf, so the empty path is the correct proof."""
    tree = MerkleAuditTree([{"seq": 1, "saga_id": "A"}])
    assert tree.root == tree.leaves[0]
    assert verify_inclusion(tree.leaves[0], [], tree.root)


def test_bundle_that_is_not_an_object():
    for junk in (None, "", [], 0, True):
        assert not verify_disclosure(junk).valid


# -- load-bearing vs informational fields -------------------------------------

def _bundle():
    records = [{"seq": i, "saga_id": "A" if i % 2 else "B", "event": "X"}
               for i in range(8)]
    return records, MerkleAuditTree(records).root, build_disclosure(records, "A")


@pytest.mark.parametrize("key", ["algorithm", "entries", "merkle_root", "saga_id"])
def test_dropping_a_load_bearing_key_invalidates(key):
    _, root, bundle = _bundle()
    b = copy.deepcopy(bundle)
    b.pop(key, None)
    assert not verify_disclosure(b, expected_root=root).valid


@pytest.mark.parametrize("key", ["disclosed", "generated_at", "log_size", "note", "version"])
def test_dropping_informational_metadata_still_verifies(key):
    """A cryptographic proof must not hinge on a counter or a timestamp. These
    fields are documented as informational in PROVENANCE.md."""
    _, root, bundle = _bundle()
    b = copy.deepcopy(bundle)
    b.pop(key, None)
    assert verify_disclosure(b, expected_root=root).valid


@pytest.mark.parametrize("field", ["leaf", "record", "path"])
def test_corrupting_a_load_bearing_entry_field_invalidates(field):
    _, root, bundle = _bundle()
    for mutate in (lambda e: e.__setitem__(field, "corrupted"),
                   lambda e: e.pop(field, None)):
        b = copy.deepcopy(bundle)
        mutate(b["entries"][0])
        assert not verify_disclosure(b, expected_root=root).valid


def test_omitting_saga_id_cannot_smuggle_other_sagas():
    """The defect this suite found: with `saga_id` absent the scope check was
    skipped, so genuine records from *other* sagas passed as this disclosure."""
    records, root, bundle = _bundle()
    tree = MerkleAuditTree(records)
    b = copy.deepcopy(bundle)
    b.pop("saga_id", None)
    # splice in a real, correctly-proven record belonging to saga B
    b_index = next(i for i, r in enumerate(records) if r["saga_id"] == "B")
    b["entries"].append({"index": b_index, "leaf": tree.leaves[b_index],
                         "record": records[b_index],
                         "path": tree.inclusion_proof(b_index)})
    result = verify_disclosure(b, expected_root=root)
    assert not result.valid
    assert any("does not name the saga" in f for f in result.failures)


def test_expected_root_mismatch_is_reported_not_raised():
    _, _, bundle = _bundle()
    result = verify_disclosure(bundle, expected_root="0" * 64)
    assert not result.valid
    assert any("root mismatch" in f for f in result.failures)
