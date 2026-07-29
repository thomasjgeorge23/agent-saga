"""Blind audit commitments: prove a record was in the log, without the record.

An auditor needs two things from a log they are not allowed to read: that a
given record was in it, and that the log has not changed since. This module
provides exactly that -- keyed HMAC commitments over records, a Merkle tree
over those commitments, and inclusion proofs a verifier can check against a
published root.

**It is not a zero-knowledge proof, and this module no longer says it is.** A
commitment scheme hides the payload from anyone without the key; a
zero-knowledge proof would let you prove statements *about* the payload while
revealing nothing. No zk-SNARK, sigma protocol, or circuit is involved here.
The earlier name promised a cryptographic guarantee this code does not
implement, which is the same class of overstatement as a rollback that reports
clean when it was partial.

Three defects in the first version are fixed here, and each is worth naming
because each silently voided the thing the module existed for:

  1. **The key was hardcoded.** `b"agent_saga_zkp_salt_32_bytes_secret"` shipped
     in the published package, so anyone could recompute commitments over
     guessed payloads and recover them by matching. For the low-entropy fields
     that actually appear in an audit log -- an amount, an email, a customer
     id -- that is a complete break. The key is now a required argument with
     no default, and short keys are refused.
  2. **`merkle_path` held the root, not a path.** An inclusion proof is the
     list of sibling hashes from the leaf up; storing the root proves nothing.
  3. **Verification was a tautology.** It asked whether the root appeared in a
     field it had itself set to the root, so a commitment with a fabricated
     hash -- or one lifted from an entirely different tree -- verified true.
     Verification now recomputes the root from the leaf and its siblings and
     compares, which is the only check that can fail.

Domain separation follows RFC 6962: leaves are hashed with a `0x00` prefix and
internal nodes with `0x01`, so an internal node can never be replayed as a
leaf. Odd levels duplicate the last node, and the duplication is recorded in
the proof so a verifier reconstructs the same shape.

`provenance.py` remains the richer selective-disclosure implementation; this
module is the small, self-contained version for handing a single root to an
external auditor.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "AuditCommitmentError",
    "BlindAuditCommitments",
    "InclusionProof",
    "ZKProofCommitment",
    "ZeroKnowledgeAuditProof",
]

MIN_KEY_BYTES = 16
"""An HMAC key shorter than 128 bits is not a secret worth relying on, and the
whole confidentiality claim rests on this one value."""

_LEAF = b"\x00"
_NODE = b"\x01"


class AuditCommitmentError(RuntimeError):
    """Raised for a missing/weak key or a malformed proof. Never returned as a
    False that a caller might read as 'not included'."""


@dataclass(frozen=True)
class InclusionProof:
    """A leaf's path to the root: sibling hashes, bottom-up.

    Each entry is `(sibling_hex, sibling_is_left)`. `sibling_is_left` matters:
    hashing the pair in the wrong order yields a different root, so the side
    has to travel with the proof.
    """

    leaf_hash: str
    siblings: Tuple[Tuple[str, bool], ...]
    leaf_index: int
    tree_size: int

    def to_dict(self) -> Dict[str, Any]:
        return {"leaf_hash": self.leaf_hash,
                "siblings": [[h, left] for h, left in self.siblings],
                "leaf_index": self.leaf_index, "tree_size": self.tree_size}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "InclusionProof":
        try:
            return cls(leaf_hash=data["leaf_hash"],
                       siblings=tuple((h, bool(left)) for h, left in data["siblings"]),
                       leaf_index=int(data["leaf_index"]),
                       tree_size=int(data["tree_size"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise AuditCommitmentError(f"malformed inclusion proof: {exc!r}") from exc


@dataclass
class ZKProofCommitment:
    """One committed record plus its inclusion proof.

    The name is kept for backwards compatibility with 0.5.0; the type is a
    commitment, not a zero-knowledge proof.
    """

    saga_id: str
    event_type: str
    commitment_hash: str
    proof: Optional[InclusionProof] = None
    timestamp: float = field(default_factory=time.time)

    @property
    def merkle_path(self) -> List[str]:
        """Sibling hashes, for callers written against the 0.5.0 field.

        Deliberately no longer contains the root: code that checked
        `root in merkle_path` was verifying nothing, and should now call
        `BlindAuditCommitments.verify()`.
        """
        return [h for h, _ in (self.proof.siblings if self.proof else ())]

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["proof"] = self.proof.to_dict() if self.proof else None
        return data


class BlindAuditCommitments:
    """Keyed commitments over records, with a Merkle tree and real proofs."""

    def __init__(self, key: bytes):
        if not isinstance(key, (bytes, bytearray)):
            raise AuditCommitmentError(
                "the commitment key must be bytes. There is no default: a key "
                "shipped inside the package would be public, and the "
                "confidentiality of every commitment rests on it.")
        if len(key) < MIN_KEY_BYTES:
            raise AuditCommitmentError(
                f"the commitment key is {len(key)} bytes; at least "
                f"{MIN_KEY_BYTES} are required. Audit payloads are low entropy "
                f"(amounts, emails, ids), so a guessable key means a verifier "
                f"can recover them by enumeration.")
        self._key = bytes(key)

    # -- commitments -------------------------------------------------------------

    def commit(self, saga_id: str, event_type: str,
               payload: Dict[str, Any]) -> str:
        """A keyed commitment to one record.

        The canonical JSON encoding is the same one the WAL hash chain uses, so
        an auditor holding the key can recompute this from recorded fields
        alone; the field separator is length-prefixed so that
        ``("a:b", "c")`` and ``("a", "b:c")`` cannot collide.
        """
        parts = [saga_id, event_type,
                 json.dumps(payload, sort_keys=True, separators=(",", ":"),
                            ensure_ascii=False, default=str)]
        message = b"".join(
            len(p.encode("utf-8")).to_bytes(8, "big") + p.encode("utf-8")
            for p in parts)
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()

    # -- tree --------------------------------------------------------------------

    def build(self, records: Sequence[Dict[str, Any]]
              ) -> Tuple[str, List[ZKProofCommitment]]:
        """Commit every record and return `(merkle_root, commitments)`, each
        commitment carrying a verifiable inclusion proof."""
        leaves = [self.commit(str(r.get("saga_id", "anon")),
                              str(r.get("event", r.get("event_type", "UNKNOWN"))),
                              r.get("payload", {}) or {})
                  for r in records]
        if not leaves:
            return hashlib.sha256(_LEAF + b"empty").hexdigest(), []

        levels = _build_levels([_leaf_hash(h) for h in leaves])
        root = levels[-1][0]

        commitments: List[ZKProofCommitment] = []
        for index, record in enumerate(records):
            commitments.append(ZKProofCommitment(
                saga_id=str(record.get("saga_id", "anon")),
                event_type=str(record.get("event", record.get("event_type", "UNKNOWN"))),
                commitment_hash=leaves[index],
                proof=InclusionProof(
                    leaf_hash=leaves[index],
                    siblings=_siblings(levels, index),
                    leaf_index=index,
                    tree_size=len(leaves)),
            ))
        return root, commitments

    # -- verification ---------------------------------------------------------------

    @staticmethod
    def verify(commitment: ZKProofCommitment, expected_root: str) -> bool:
        """Recompute the root from the leaf and its siblings, and compare.

        This is the check the 0.5.0 version did not perform: a fabricated
        commitment hash, or a real one lifted from a different tree, now fails.
        Returns False for a wrong proof and raises only when the proof is
        structurally unusable -- a caller must never read "malformed" as
        "not included".
        """
        proof = commitment.proof
        if proof is None:
            raise AuditCommitmentError(
                "commitment carries no inclusion proof; it cannot be verified. "
                "Rebuild it with BlindAuditCommitments.build().")
        if proof.leaf_hash != commitment.commitment_hash:
            return False

        computed = _leaf_hash(proof.leaf_hash)
        for sibling, sibling_is_left in proof.siblings:
            try:
                pair = ((sibling, computed) if sibling_is_left
                        else (computed, sibling))
                computed = _node_hash(*pair)
            except (TypeError, ValueError) as exc:
                raise AuditCommitmentError(f"malformed sibling: {exc!r}") from exc
        return hmac.compare_digest(computed, expected_root)

    def verify_record(self, record: Dict[str, Any],
                      commitment: ZKProofCommitment, expected_root: str) -> bool:
        """The key-holder's check: that this *specific* payload is the one
        committed, and that the commitment is in the tree. An auditor without
        the key can only do the second half."""
        recomputed = self.commit(
            str(record.get("saga_id", "anon")),
            str(record.get("event", record.get("event_type", "UNKNOWN"))),
            record.get("payload", {}) or {})
        if not hmac.compare_digest(recomputed, commitment.commitment_hash):
            return False
        return self.verify(commitment, expected_root)


class ZeroKnowledgeAuditProof(BlindAuditCommitments):
    """Deprecated alias for :class:`BlindAuditCommitments`.

    Kept so 0.5.0 imports keep working, with two behaviour changes that could
    not be preserved without keeping the vulnerability: `audit_salt` is now
    **required** (there is no safe default to fall back to), and
    `verify_commitment_against_root` performs a real verification, so a
    forged commitment that previously passed now fails.
    """

    def __init__(self, audit_salt: Optional[bytes] = None):
        if audit_salt is None:
            raise AuditCommitmentError(
                "audit_salt is now required. Releases up to 0.5.0 fell back to "
                "a constant compiled into the package, which made every "
                "commitment recoverable by anyone who had installed it. Pass "
                "a secret of at least 16 bytes, e.g. os.urandom(32), and keep "
                "it with your audit keys.")
        super().__init__(audit_salt)

    def create_record_commitment(self, saga_id: str, event_type: str,
                                 payload: Dict[str, Any]) -> str:
        return self.commit(saga_id, event_type, payload)

    def generate_proof_tree(self, records: Sequence[Dict[str, Any]]
                            ) -> Tuple[str, List[ZKProofCommitment]]:
        return self.build(records)

    def verify_commitment_against_root(self, commitment: ZKProofCommitment,
                                       expected_root: str) -> bool:
        return self.verify(commitment, expected_root)


# -- tree internals ---------------------------------------------------------------

def _leaf_hash(commitment_hex: str) -> str:
    return hashlib.sha256(_LEAF + commitment_hex.encode("utf-8")).hexdigest()


def _node_hash(left: str, right: str) -> str:
    return hashlib.sha256(
        _NODE + left.encode("utf-8") + right.encode("utf-8")).hexdigest()


def _build_levels(leaves: List[str]) -> List[List[str]]:
    levels = [list(leaves)]
    while len(levels[-1]) > 1:
        current = levels[-1]
        nxt: List[str] = []
        for i in range(0, len(current), 2):
            left = current[i]
            right = current[i + 1] if i + 1 < len(current) else current[i]
            nxt.append(_node_hash(left, right))
        levels.append(nxt)
    return levels


def _siblings(levels: List[List[str]], index: int) -> Tuple[Tuple[str, bool], ...]:
    """Sibling hashes bottom-up, each flagged with whether it sits on the left.

    A lone node at the end of an odd level is paired with itself, which is
    recorded as a right-hand sibling equal to the node -- the verifier then
    reproduces the same duplication rather than guessing at the shape.
    """
    path: List[Tuple[str, bool]] = []
    position = index
    for level in levels[:-1]:
        if position % 2 == 0:
            sibling = level[position + 1] if position + 1 < len(level) else level[position]
            path.append((sibling, False))
        else:
            path.append((level[position - 1], True))
        position //= 2
    return tuple(path)
