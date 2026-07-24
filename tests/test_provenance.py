"""Cryptographic selective disclosure: prove ONE saga to an auditor without
revealing any other saga, and prove the disclosure came from the committed log."""

import copy
import json

import pytest

from agent_saga.provenance import (
    ALGORITHM, MerkleAuditTree, audit_root, build_disclosure, leaf_hash,
    verify_disclosure, verify_inclusion)


def _log():
    """3 sagas interleaved; 7 records exercises odd-level padding."""
    return [
        {"seq": 1, "saga_id": "saga-A", "event": "SAGA_START", "ts": 1.0},
        {"seq": 2, "saga_id": "saga-B", "event": "SAGA_START", "customer": "SECRET-B"},
        {"seq": 3, "saga_id": "saga-A", "event": "STEP_COMMITTED", "tool": "stripe.charge", "amount": 100},
        {"seq": 4, "saga_id": "saga-C", "event": "SAGA_START", "ssn": "SECRET-C"},
        {"seq": 5, "saga_id": "saga-A", "event": "ROLLBACK_STEP", "handler": "stripe.refund"},
        {"seq": 6, "saga_id": "saga-B", "event": "SAGA_COMPLETE"},
        {"seq": 7, "saga_id": "saga-A", "event": "SAGA_ABORTED"},
    ]


# -- tree ---------------------------------------------------------------------

def test_every_leaf_proves_inclusion():
    recs = _log()
    tree = MerkleAuditTree(recs)
    for i in range(len(recs)):
        assert verify_inclusion(tree.leaves[i], tree.inclusion_proof(i), tree.root)


def test_root_is_deterministic_and_order_sensitive():
    recs = _log()
    assert audit_root(recs) == audit_root(list(recs))          # deterministic
    swapped = [recs[1], recs[0]] + recs[2:]
    assert audit_root(swapped) != audit_root(recs)             # order is committed


def test_empty_log_and_single_record():
    assert audit_root([]) == "0" * 64
    one = [{"seq": 1, "saga_id": "s", "event": "X"}]
    t = MerkleAuditTree(one)
    assert t.size == 1 and verify_inclusion(t.leaves[0], t.inclusion_proof(0), t.root)


def test_inclusion_proof_index_bounds():
    t = MerkleAuditTree(_log())
    with pytest.raises(IndexError):
        t.inclusion_proof(99)


# -- selective disclosure -----------------------------------------------------

def test_disclosure_verifies_against_published_root():
    recs = _log()
    root = audit_root(recs)
    bundle = build_disclosure(recs, "saga-A")
    res = verify_disclosure(bundle, expected_root=root)
    assert res.valid and res.disclosed == 4 and res.verified == 4
    assert "VERIFIED" in res.summary()


def test_disclosure_leaks_nothing_about_other_sagas():
    recs = _log()
    blob = json.dumps(build_disclosure(recs, "saga-A"), default=str)
    # other customers' data never appears -- only opaque sibling hashes do
    assert "SECRET-B" not in blob and "SECRET-C" not in blob
    assert "saga-B" not in blob and "saga-C" not in blob


# -- adversarial --------------------------------------------------------------

def test_tampered_record_is_detected():
    recs = _log()
    root = audit_root(recs)
    bundle = copy.deepcopy(build_disclosure(recs, "saga-A"))
    for e in bundle["entries"]:
        if e["record"].get("amount"):
            e["record"]["amount"] = 999999          # hide a big charge
    res = verify_disclosure(bundle, expected_root=root)
    assert not res.valid
    assert any("does not match its leaf hash" in f for f in res.failures)


def test_forged_record_never_in_the_log_is_rejected():
    recs = _log()
    root = audit_root(recs)
    bundle = copy.deepcopy(build_disclosure(recs, "saga-A"))
    fake = {"seq": 99, "saga_id": "saga-A", "event": "STEP_COMMITTED", "tool": "fabricated"}
    bundle["entries"].append({"index": 99, "leaf": leaf_hash(fake), "record": fake,
                              "path": bundle["entries"][0]["path"]})
    res = verify_disclosure(bundle, expected_root=root)
    assert not res.valid
    assert any("does not reach the root" in f for f in res.failures)


def test_bundle_from_a_doctored_log_fails_against_published_root():
    recs = _log()
    published = audit_root(recs)
    doctored = [r for r in recs if r["seq"] != 3]      # delete the charge entirely
    bundle = build_disclosure(doctored, "saga-A")
    res = verify_disclosure(bundle, expected_root=published)
    assert not res.valid
    assert any("root mismatch" in f for f in res.failures)


def test_record_from_another_saga_smuggled_in_is_rejected():
    recs = _log()
    tree = MerkleAuditTree(recs)
    bundle = build_disclosure(recs, "saga-A")
    # splice in a genuine, correctly-proven record that belongs to saga-B
    bundle["entries"].append({"index": 1, "leaf": tree.leaves[1],
                              "record": recs[1], "path": tree.inclusion_proof(1)})
    res = verify_disclosure(bundle, expected_root=tree.root)
    assert not res.valid
    assert any("different saga" in f for f in res.failures)


def test_unknown_algorithm_is_refused():
    bundle = build_disclosure(_log(), "saga-A")
    bundle["algorithm"] = "rot13"
    assert not verify_disclosure(bundle).valid


def test_leaf_and_node_hashes_are_domain_separated():
    # An internal node must never verify as if it were a leaf (2nd-preimage).
    t = MerkleAuditTree(_log())
    internal = t._levels[1][0]
    assert internal not in t.leaves


# -- CLI ----------------------------------------------------------------------

def _write_log(tmp_path):
    p = tmp_path / "audit.wal"
    p.write_text("\n".join(json.dumps(r) for r in _log()) + "\n", encoding="utf-8")
    return p


def test_cli_audit_root_prove_and_verify(tmp_path, capsys):
    from agent_saga.cli import main
    wal = _write_log(tmp_path)

    # 1. publish the commitment
    assert main(["audit-root", "--wal", str(wal)]) == 0
    root = capsys.readouterr().out.strip()
    assert len(root) == 64

    # 2. emit a disclosure for one saga
    proof = tmp_path / "proof.json"
    assert main(["prove", "saga-A", "--wal", str(wal), "--out", str(proof)]) == 0
    bundle = json.loads(proof.read_text(encoding="utf-8"))
    assert bundle["disclosed"] == 4 and bundle["merkle_root"] == root

    # 3. auditor verifies against the published root
    assert main(["verify-proof", str(proof), "--root", root]) == 0
    assert "VERIFIED" in capsys.readouterr().out


def test_cli_verify_proof_rejects_tampering(tmp_path, capsys):
    from agent_saga.cli import main
    wal = _write_log(tmp_path)
    main(["audit-root", "--wal", str(wal)])
    root = capsys.readouterr().out.strip()
    proof = tmp_path / "p.json"
    main(["prove", "saga-A", "--wal", str(wal), "--out", str(proof)])
    capsys.readouterr()

    bundle = json.loads(proof.read_text(encoding="utf-8"))
    for e in bundle["entries"]:
        if e["record"].get("amount"):
            e["record"]["amount"] = 1
    proof.write_text(json.dumps(bundle), encoding="utf-8")

    assert main(["verify-proof", str(proof), "--root", root]) == 1   # CI-gateable
    assert "FAILED" in capsys.readouterr().out


def test_cli_prove_unknown_saga(tmp_path):
    from agent_saga.cli import main
    assert main(["prove", "nope", "--wal", str(_write_log(tmp_path))]) == 1


# -- the documented independent verifier (PROVENANCE.md §6) -------------------

def _documented_canonical(obj):
    """Verbatim from PROVENANCE.md. If this drifts from the implementation, an
    auditor following the doc gets wrong hashes -- so it is pinned here."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str).encode("utf-8")


def _documented_leaf(record):
    import hashlib
    return hashlib.sha256(b"\x00" + _documented_canonical(record)).hexdigest()


def _documented_node(left, right):
    import hashlib
    return hashlib.sha256(b"\x01" + bytes.fromhex(left) + bytes.fromhex(right)).hexdigest()


def test_documented_verifier_matches_implementation():
    # ASCII, non-ASCII, and nested -- the non-ASCII case is the one that breaks
    # if ensure_ascii is left at its default.
    for rec in (
        {"seq": 1, "saga_id": "s", "event": "SAGA_START"},
        {"seq": 2, "saga_id": "s", "customer": "Renée Müller", "note": "café"},
        {"seq": 3, "saga_id": "s", "nested": {"b": 2, "a": 1}, "amount": 42.5},
    ):
        assert _documented_leaf(rec) == leaf_hash(rec)


def test_documented_verifier_validates_a_real_bundle():
    """An auditor following PROVENANCE.md §6 with nothing but hashlib+json must
    be able to verify a bundle we produced."""
    recs = _log()
    recs[1]["customer"] = "Renée Müller"          # force the non-ASCII path
    published_root = audit_root(recs)
    bundle = build_disclosure(recs, "saga-A")

    for entry in bundle["entries"]:
        h = _documented_leaf(entry["record"])
        assert h == entry["leaf"]
        for sibling, side in entry["path"]:
            h = _documented_node(sibling, h) if side == "L" else _documented_node(h, sibling)
        assert h == published_root                # independently verified
