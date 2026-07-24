"""Cryptographic selective disclosure for the WAL.

The hash chain in `integrity.py` proves the log has not been altered *as a
whole*. It cannot answer the question a regulator actually asks:

    "Prove to me that transaction X happened, and was properly compensated --
     without showing me every other customer's transactions."

Handing over the whole WAL to prove one saga is a data-protection incident.
Handing over an extract proves nothing, because an extract can be fabricated.

This module closes that gap. It builds a Merkle tree over the WAL records and
publishes a single 32-byte root as the commitment. Later, for any one saga, it
emits a *disclosure bundle*: that saga's records plus an inclusion proof for
each. An auditor verifies every disclosed record really is an unaltered member
of the committed log -- and learns nothing about any record that was not
disclosed, because a sibling hash reveals nothing about the data under it.

    root = MerkleAuditTree(records).root          # publish/notarise this once
    bundle = build_disclosure(records, "saga-42") # give this to the auditor
    verify_disclosure(bundle)                     # auditor runs this

The root is the only thing that must be published. Anchor it wherever your
compliance regime wants it durable -- a transparency log, a notary, a chain, or
simply a countersigned email -- and every future disclosure is checkable against
it. Tampering with a disclosed record, or fabricating one that was never in the
log, fails verification.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

from .integrity import canonical

PROOF_VERSION = 1
ALGORITHM = "sha256-merkle-v1"

# Domain separation: leaves and internal nodes are hashed with different
# prefixes so an attacker cannot present an internal node as if it were a leaf
# (the classic second-preimage attack on naive Merkle trees).
_LEAF_PREFIX = b"\x00"
_NODE_PREFIX = b"\x01"


def leaf_hash(record: dict) -> str:
    """The commitment to a single WAL record: SHA-256 over its canonical form.
    Any change to any field -- including the chain fields -- changes this."""
    return hashlib.sha256(_LEAF_PREFIX + canonical(record)).hexdigest()


def _node_hash(left: str, right: str) -> str:
    return hashlib.sha256(
        _NODE_PREFIX + bytes.fromhex(left) + bytes.fromhex(right)).hexdigest()


class MerkleAuditTree:
    """A Merkle tree over WAL records. `root` is the published commitment."""

    def __init__(self, records: Sequence[dict]):
        self.leaves: list[str] = [leaf_hash(r) for r in records]
        self._levels: list[list[str]] = self._build(self.leaves)

    @staticmethod
    def _build(leaves: Sequence[str]) -> list[list[str]]:
        if not leaves:
            return [[]]
        levels = [list(leaves)]
        while len(levels[-1]) > 1:
            cur = levels[-1]
            # Odd node count: promote the last node by pairing it with itself.
            if len(cur) % 2:
                cur = cur + [cur[-1]]
                levels[-1] = cur
            levels.append([_node_hash(cur[i], cur[i + 1])
                           for i in range(0, len(cur), 2)])
        return levels

    @property
    def root(self) -> str:
        """The commitment. Empty log commits to the all-zero root."""
        top = self._levels[-1]
        return top[0] if top else "0" * 64

    @property
    def size(self) -> int:
        return len(self.leaves)

    def inclusion_proof(self, index: int) -> list[list[str]]:
        """The sibling path proving leaf `index` is in the tree. Each element is
        ``[sibling_hash, "L"|"R"]`` -- the side the *sibling* sits on."""
        if not (0 <= index < len(self.leaves)):
            raise IndexError(f"leaf index {index} out of range (size {len(self.leaves)})")
        path: list[list[str]] = []
        idx = index
        for level in self._levels[:-1]:
            # `_build` already padded odd levels, but a level can still be odd
            # here if it is the level we are standing on; mirror the padding.
            sib_idx = idx ^ 1
            if sib_idx >= len(level):
                sib_idx = idx           # self-pairing
            side = "L" if sib_idx < idx else "R"
            path.append([level[sib_idx], side])
            idx //= 2
        return path


_HEX = set("0123456789abcdef")


def _is_hash(value: Any) -> bool:
    """A well-formed lowercase sha256 hex digest. Anything else is not a hash we
    will compute with -- the value came from the party being audited."""
    return (isinstance(value, str) and len(value) == 64
            and all(c in _HEX for c in value))


def verify_inclusion(leaf: Any, path: Any, root: Any) -> bool:
    """Recompute the root from a leaf and its sibling path.

    Every input is untrusted, so a malformed path is a failed proof rather than
    an exception. Returns False for anything that is not a well-formed chain of
    sha256 siblings ending at `root`.
    """
    if not (_is_hash(leaf) and _is_hash(root)):
        return False
    if not isinstance(path, (list, tuple)):
        return False
    h = leaf
    for step in path:
        if not isinstance(step, (list, tuple)) or len(step) != 2:
            return False
        sibling, side = step
        if not _is_hash(sibling) or side not in ("L", "R"):
            return False
        h = _node_hash(sibling, h) if side == "L" else _node_hash(h, sibling)
    return hmac.compare_digest(h, root)


@dataclass
class DisclosureResult:
    valid: bool
    root: str
    disclosed: int = 0
    verified: int = 0
    failures: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.valid:
            return (f"disclosure VERIFIED: {self.verified}/{self.disclosed} record(s) "
                    f"proven against root {self.root[:16]}...")
        return (f"disclosure FAILED: {len(self.failures)} problem(s); "
                f"{self.verified}/{self.disclosed} verified")


def build_disclosure(records: Sequence[dict], saga_id: str, *,
                     note: str = "") -> dict:
    """Build a selective-disclosure bundle for one saga.

    The bundle carries only that saga's records -- every other record in the log
    contributes nothing but an opaque sibling hash, so the auditor learns their
    existence and count, never their contents."""
    tree = MerkleAuditTree(records)
    entries = []
    for i, rec in enumerate(records):
        if rec.get("saga_id") != saga_id:
            continue
        entries.append({
            "index": i,
            "leaf": tree.leaves[i],
            "record": rec,
            "path": tree.inclusion_proof(i),
        })
    return {
        "version": PROOF_VERSION,
        "algorithm": ALGORITHM,
        "merkle_root": tree.root,
        "log_size": tree.size,
        "saga_id": saga_id,
        "disclosed": len(entries),
        "generated_at": time.time(),
        "note": note,
        "entries": entries,
    }


def verify_disclosure(bundle: dict, *, expected_root: Optional[str] = None) -> DisclosureResult:
    """Verify every disclosed record against the bundle's Merkle root.

    Pass ``expected_root`` -- the root you published/notarised earlier -- to also
    prove the bundle was built from *that* log rather than one the discloser
    made up. Without it, the bundle is only internally consistent.
    """
    # The bundle is supplied by the party being audited, so nothing in it is
    # trusted to be well-formed. Every access below is defensive: a malformed
    # bundle must come back as *invalid*, never as an exception. A verifier that
    # crashes on hostile input gives the discloser a way to turn a failed proof
    # into what looks like a broken tool.
    if not isinstance(bundle, Mapping):
        return DisclosureResult(valid=False, root="",
                                failures=["bundle is not an object"])

    raw_root = bundle.get("merkle_root")
    root = raw_root if _is_hash(raw_root) else ""
    raw_entries = bundle.get("entries")
    entries = raw_entries if isinstance(raw_entries, (list, tuple)) else []
    result = DisclosureResult(valid=False, root=root, disclosed=len(entries))

    if bundle.get("algorithm") != ALGORITHM:
        result.failures.append(f"unknown algorithm {bundle.get('algorithm')!r}")
        return result
    if not root:
        result.failures.append("bundle carries no usable merkle_root")
        return result
    if expected_root is not None and not hmac.compare_digest(root, str(expected_root)):
        result.failures.append(
            f"root mismatch: bundle {root[:16]}... != expected {str(expected_root)[:16]}...")
        return result
    if not isinstance(raw_entries, (list, tuple)):
        result.failures.append("bundle carries no entries list")
        return result

    # A bundle claims to be about one saga. If that claim is absent we cannot
    # check scope, and silently skipping the check would let a discloser omit
    # `saga_id` to smuggle in records belonging to other sagas.
    claimed_saga = bundle.get("saga_id")
    if not isinstance(claimed_saga, str) or not claimed_saga:
        result.failures.append("bundle does not name the saga it discloses")
        return result

    for position, e in enumerate(entries):
        if not isinstance(e, Mapping):
            result.failures.append(f"entry {position}: not an object")
            continue
        idx = e.get("index", position)
        rec = e.get("record")
        claimed_leaf = e.get("leaf")

        if not isinstance(rec, Mapping):
            result.failures.append(f"entry {idx}: record is not an object")
            continue
        if not _is_hash(claimed_leaf):
            result.failures.append(f"entry {idx}: leaf is not a sha256 hash")
            continue
        # 1. the record must actually hash to the leaf it claims
        if not hmac.compare_digest(leaf_hash(dict(rec)), claimed_leaf):
            result.failures.append(f"record {idx}: content does not match its leaf hash")
            continue
        # 2. the leaf must be provably in the committed tree
        if not verify_inclusion(claimed_leaf, e.get("path") or [], root):
            result.failures.append(f"record {idx}: inclusion proof does not reach the root")
            continue
        # 3. it must belong to the saga this bundle claims to be about
        if rec.get("saga_id") != claimed_saga:
            result.failures.append(f"record {idx}: belongs to a different saga")
            continue
        result.verified += 1

    result.valid = bool(entries) and result.verified == len(entries) and not result.failures
    return result


def audit_root(records: Sequence[dict]) -> str:
    """Convenience: the commitment for a whole log."""
    return MerkleAuditTree(records).root


__all__ = [
    "MerkleAuditTree", "DisclosureResult",
    "leaf_hash", "verify_inclusion", "build_disclosure", "verify_disclosure",
    "audit_root", "PROOF_VERSION", "ALGORITHM",
]
