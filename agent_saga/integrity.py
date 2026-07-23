"""Tamper-evident WAL: a hash chain over the log, and the tools to prove it.

A write-ahead log is already the record of what an agent did with real money.
Chained, it becomes something an auditor can rely on: any edit, reorder,
insertion, or deletion of a record invalidates every hash after it, and the
verifier names the first record where the log stops adding up.

Three properties of *this* codebase shaped the format, and each one breaks a
naive `hash(prev + record)` chain:

  * **Compaction deletes records.** `FileWAL.compact()` legitimately drops
    settled sagas. A chain that treats every gap as tampering would fail on a
    healthy log, and a chain re-computed after compaction would let anyone erase
    evidence and re-stamp it. So a gap is valid only when an *attestation
    record* -- itself chained -- says which sequence range left and what its
    hashes were.

  * **GDPR requires erasing payloads from history.** So the chain never hashes
    the payload directly. It hashes a `_cd` (content digest) computed over the
    payload; redaction drops the payload and keeps `_cd`, and the chain still
    verifies end to end. What survives is proof that a record existed, when, in
    what order, and of what type -- with its contents provably gone.

  * **Low-entropy payloads are brute-forceable.** `{"amount": 4200}` has few
    plausible preimages, so an unsalted digest would leak the value it was
    supposed to erase. Each record carries a random `_s` (salt) folded into
    `_cd`; redaction deletes the salt, which turns the digest into a one-way
    dead end rather than a lookup table.

SCOPE: the chain proves that *one writer's* log is intact. It is per-process by
construction -- a single chain across nodes would need a global lock on every
append, which is the throughput of a distributed transaction on the hot path of
every tool call. For a fleet, each node's log is independently provable and
correlating them is a control-plane concern, not a WAL one.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

GENESIS = "0" * 64
"""The hash a chain starts from. Not a real digest -- a fixed anchor, so the
first record's hash still depends on something and cannot be forged by simply
declaring itself first."""

HASH_FIELD = "_h"
PREV_FIELD = "_ph"
DIGEST_FIELD = "_cd"
SALT_FIELD = "_s"
REDACTED_FIELD = "_redacted"
GAP_EVENT = "WAL_CHAIN_GAP"

_META_FIELDS = frozenset({HASH_FIELD, PREV_FIELD, DIGEST_FIELD, SALT_FIELD,
                          REDACTED_FIELD})

SALT_BYTES = 16


def canonical(obj: Any) -> bytes:
    """Bytes that must not change for the same logical content.

    `sort_keys` because dict ordering is an implementation detail and a rehash
    on a different Python must match. `separators` because whitespace is not
    content. `default=str` because a payload that cannot be serialized must
    still hash to *something* deterministic rather than raise inside the flusher
    thread and take the log down.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str).encode("utf-8")


def content_digest(payload: dict, salt: str) -> str:
    """Digest of a record's business content, salted.

    The salt is what makes redaction safe: without it, `{"amount": 4200}` is
    recoverable from its own digest by trying every plausible amount.
    """
    return hashlib.sha256(salt.encode("ascii") + canonical(payload)).hexdigest()


def record_hash(prev_hash: str, seq: Any, ts: Any, event: str, digest: str) -> str:
    """Link one record to its predecessor.

    Deliberately over the *header* (seq, ts, event) plus the content digest, not
    over the raw record: that is precisely what lets a payload be redacted later
    without breaking every hash that follows.
    """
    return hashlib.sha256(canonical({
        "prev": prev_hash, "seq": seq, "ts": ts, "event": event, "cd": digest,
    })).hexdigest()


def business_fields(record: dict) -> dict:
    """The record minus chain metadata and the header already hashed by name."""
    return {k: v for k, v in record.items()
            if k not in _META_FIELDS and k not in ("seq", "ts", "event")}


def stamp(record: dict, prev_hash: str) -> str:
    """Chain one record in place. Returns the new head.

    Called on the single flusher thread in sequence order, so no lock is needed
    and the chain cannot interleave.
    """
    salt = os.urandom(SALT_BYTES).hex()
    digest = content_digest(business_fields(record), salt)
    this = record_hash(prev_hash, record.get("seq"), record.get("ts"),
                       record.get("event", ""), digest)
    record[SALT_FIELD] = salt
    record[DIGEST_FIELD] = digest
    # The predecessor's hash is stored, not merely implied by position. It is
    # what lets a record still prove itself after compaction has removed the
    # record it was chained to -- without it, deleting one settled saga would
    # make every survivor after it unverifiable, and the honest housekeeping
    # this log supports would be indistinguishable from an attack.
    record[PREV_FIELD] = prev_hash
    record[HASH_FIELD] = this
    return this


def stamp_batch(records: Iterable[dict], prev_hash: str) -> str:
    head = prev_hash
    for record in records:
        head = stamp(record, head)
    return head


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

@dataclass
class ChainBreak:
    seq: Any
    index: int
    reason: str
    expected: Optional[str] = None
    found: Optional[str] = None

    def __str__(self) -> str:
        detail = ""
        if self.expected is not None:
            detail = f" (expected {self.expected[:16]}..., found {(self.found or 'none')[:16]}...)"
        return f"record #{self.index} (seq={self.seq}): {self.reason}{detail}"


@dataclass
class ChainReport:
    """What an auditor reads. `intact` is the only word that matters."""

    checked: int = 0
    unchained: int = 0
    redacted: int = 0
    attested_gaps: int = 0
    head: str = GENESIS
    breaks: list[ChainBreak] = field(default_factory=list)

    @property
    def intact(self) -> bool:
        return not self.breaks

    def summary(self) -> str:
        if self.intact:
            parts = [f"chain intact across {self.checked} record(s)"]
            if self.redacted:
                parts.append(f"{self.redacted} redacted")
            if self.attested_gaps:
                parts.append(f"{self.attested_gaps} attested gap(s)")
            if self.unchained:
                parts.append(f"{self.unchained} unchained (pre-chain records)")
            return ", ".join(parts) + f"; head {self.head[:16]}..."
        return (f"CHAIN BROKEN at {len(self.breaks)} point(s) across "
                f"{self.checked} record(s) -- first: {self.breaks[0]}")


def verify(records: Iterable[dict], *, start: str = GENESIS,
           strict: bool = False) -> ChainReport:
    """Recompute the chain and report the first place it stops adding up.

    `strict` refuses a log that contains records with no hash at all. The
    default tolerates them so a log written before chaining was enabled still
    verifies from the point chaining began -- silently failing every
    pre-existing log would push operators to turn verification off, which is
    worse than a partial proof honestly labelled.
    """
    materialized = list(records)
    # Attestations live at the tail (splicing them in at the gap would require
    # re-hashing everything after it), so they must be collected before the
    # walk that consults them.
    accounted = attested_seqs(materialized)

    report = ChainReport(head=start)
    prev = start
    expected_seq = 1        # BufferedWAL numbers its first append 1.

    for index, record in enumerate(materialized):
        report.checked += 1
        seq = record.get("seq")

        # A gap in sequence is tampering unless an attestation accounts for
        # every sequence number inside it.
        gap_forgiven = False
        if isinstance(seq, int) and seq > expected_seq:
            unaccounted = sorted(set(range(expected_seq, seq)) - accounted)
            if unaccounted:
                shown = unaccounted[:5]
                more = "" if len(unaccounted) <= 5 else f" (+{len(unaccounted) - 5} more)"
                report.breaks.append(ChainBreak(
                    seq, index,
                    f"{len(unaccounted)} record(s) missing with no {GAP_EVENT} "
                    f"attestation: seq {shown}{more}"))
            else:
                report.attested_gaps += 1
                gap_forgiven = True
        if isinstance(seq, int):
            expected_seq = seq + 1

        stored = record.get(HASH_FIELD)
        if stored is None:
            report.unchained += 1
            if strict:
                report.breaks.append(ChainBreak(seq, index, "record carries no hash"))
            continue

        digest = record.get(DIGEST_FIELD)
        if digest is None:
            report.breaks.append(ChainBreak(seq, index, "record carries no content digest"))
            prev = stored
            continue

        # 1. Content. A record still holding its payload must match its own
        #    digest; a redacted one cannot, and says so.
        if record.get(REDACTED_FIELD):
            report.redacted += 1
        else:
            salt = record.get(SALT_FIELD)
            if salt is None:
                report.breaks.append(ChainBreak(
                    seq, index, "salt missing but record is not marked redacted"))
            else:
                recomputed = content_digest(business_fields(record), salt)
                if recomputed != digest:
                    report.breaks.append(ChainBreak(
                        seq, index, "payload does not match its content digest "
                        "(a field was altered)", digest, recomputed))

        # 2. Self-consistency. The record's own hash must follow from the
        #    predecessor hash it *claims*. This is what catches an edited header
        #    or an edited _ph, independently of what came before it in the file.
        claimed_prev = record.get(PREV_FIELD, prev)
        expected_hash = record_hash(claimed_prev, seq, record.get("ts"),
                                    record.get("event", ""), digest)
        if expected_hash != stored:
            report.breaks.append(ChainBreak(
                seq, index, "record hash does not match its own contents "
                "(a field or the chain pointer was altered)", expected_hash, stored))

        # 3. Linkage. The predecessor it claims must be the record actually
        #    before it -- unless an attested gap explains the discontinuity.
        #    Splitting this from (2) is what lets compaction remove a record
        #    without making every survivor after it unverifiable.
        elif claimed_prev != prev and not gap_forgiven:
            report.breaks.append(ChainBreak(
                seq, index, "record does not link to the one before it "
                "(insertion, reorder, or unattested deletion)", prev, claimed_prev))

        # Continue from what the log *claims*, so one break yields one finding
        # rather than cascading a false break onto every record after it.
        prev = stored

    report.head = prev
    return report


# ---------------------------------------------------------------------------
# Redaction (GDPR erasure that keeps the chain provable)
# ---------------------------------------------------------------------------

def redact_record(record: dict, *, reason: str = "erasure-request") -> dict:
    """Erase a record's contents, keeping its place in the chain provable.

    Afterwards the log still proves this record existed, when, in what order,
    and of what type -- and proves that nothing else was touched. What it can no
    longer reveal is what was in it, which is the point.

    Irreversible: the salt is destroyed, so the digest cannot be walked back to
    the payload even by whoever holds the log.
    """
    if record.get(HASH_FIELD) is None:
        raise ValueError(
            "cannot redact an unchained record: without a stored content digest "
            "there is nothing left to prove the record by, so erasing it is "
            "indistinguishable from deleting it")
    kept = {k: record[k] for k in ("seq", "ts", "event") if k in record}
    kept[HASH_FIELD] = record[HASH_FIELD]
    kept[DIGEST_FIELD] = record[DIGEST_FIELD]
    if PREV_FIELD in record:
        kept[PREV_FIELD] = record[PREV_FIELD]
    kept[REDACTED_FIELD] = reason
    return kept


def _has_dotted_path(record: dict, path: str) -> bool:
    parts = path.split(".")
    curr = record
    for p in parts:
        if isinstance(curr, dict) and p in curr:
            curr = curr[p]
        else:
            return False
    return True


REDACTED_VALUE = "[REDACTED]"


def _redact_dotted_path(record: dict, path: str) -> Optional[dict]:
    """Return a copy of `record` with the value at `path` masked, or None if the
    path is absent. Only the leaf is touched; the rest of the record -- the
    surrounding audit context an operator still needs -- is preserved.

    Copies are made along the traversed path so the caller's nested dicts are
    never mutated in place."""
    import copy

    parts = path.split(".")
    if not _has_dotted_path(record, path):
        return None
    new_record = dict(record)
    cursor = new_record
    for p in parts[:-1]:
        child = dict(cursor[p])   # copy each level we descend into
        cursor[p] = child
        cursor = child
    cursor[parts[-1]] = REDACTED_VALUE
    return new_record


def redact_where(records: list[dict], predicate, *,
                 reason: str = "erasure-request") -> tuple[list[dict], int]:
    """Redact records, returning (records, count).

    Two modes, chosen by the type of `predicate`:

    * **Callable** -- whole-record GDPR erasure. Every record the predicate
      selects is replaced by a redacted stub that keeps its place in the hash
      chain provable (see ``redact_record``). For already-chained WAL records.

    * **Dotted-path string** (e.g. ``"kwargs.card.cvv"``) -- surgical masking of
      one nested field, keeping the rest of the record intact. This is the
      pre-WAL scrub: strip a nested credential out of a payload *before* it is
      written, so it never reaches disk. Works on unchained records (they have
      no digest to preserve yet)."""
    out, count = [], 0

    if isinstance(predicate, str):
        path_str = predicate
        for record in records:
            masked = _redact_dotted_path(record, path_str)
            if masked is not None:
                out.append(masked)
                count += 1
            else:
                out.append(record)
        return out, count

    for record in records:
        if predicate(record) and not record.get(REDACTED_FIELD):
            out.append(redact_record(record, reason=reason))
            count += 1
        else:
            out.append(record)
    return out, count


def as_runs(seqs: Iterable[int]) -> list[list[int]]:
    """Compress sequence numbers into [start, end] runs.

    Compaction keeps sagas, not ranges, so what it removes is scattered. Listing
    every missing sequence would make the attestation larger than the records it
    describes on a long-lived log.
    """
    ordered = sorted(int(s) for s in seqs)
    runs: list[list[int]] = []
    for seq in ordered:
        if runs and seq == runs[-1][1] + 1:
            runs[-1][1] = seq
        else:
            runs.append([seq, seq])
    return runs


def gap_attestation(removed_seqs: Iterable[int], removed_digest: str,
                    reason: str) -> dict:
    """The record that makes a deletion honest.

    Compaction dropping settled sagas is legitimate housekeeping. Without this,
    a verifier cannot distinguish it from an attacker deleting the record of a
    charge -- both look like missing sequence numbers.

    The attestation names exactly which sequences left and the digest of what
    they were, and is itself chained at the tail, so removing or editing the
    attestation breaks the chain in turn. It is appended rather than spliced in
    at the gap because splicing would require re-hashing every record after it,
    which is precisely the power a tamper-evident log must not hand out.
    """
    runs = as_runs(removed_seqs)
    return {
        "event": GAP_EVENT,
        "ranges": runs,
        "removed": sum(b - a + 1 for a, b in runs),
        "removed_digest": removed_digest,
        "reason": reason,
    }


def attested_seqs(records: Iterable[dict]) -> set[int]:
    """Every sequence number some attestation in the log accounts for."""
    out: set[int] = set()
    for record in records:
        if record.get("event") != GAP_EVENT:
            continue
        for run in record.get("ranges") or []:
            try:
                start, end = int(run[0]), int(run[1])
            except (TypeError, ValueError, IndexError):
                continue
            if 0 <= end - start <= 10_000_000:
                out.update(range(start, end + 1))
    return out


def digest_of(records: Iterable[dict]) -> str:
    """A single digest over a set of records, for attestations and manifests."""
    h = hashlib.sha256()
    for record in records:
        h.update((record.get(HASH_FIELD) or content_digest(record, "")).encode("ascii"))
    return h.hexdigest()


# ---------------------------------------------------------------------------
# WORM export
# ---------------------------------------------------------------------------

def export_worm(records: Iterable[dict], out_dir: str, *,
                source: str = "", report: Optional[ChainReport] = None) -> dict:
    """Write a self-describing bundle for write-once storage.

    The bundle is deliberately plain: newline-delimited JSON plus a manifest.
    An auditor should be able to re-verify it years from now with this library
    absent, using nothing but `sha256sum` and the documented rule -- an archive
    that can only be read by the tool that wrote it is not evidence, it is a
    dependency.

    The manifest hashes the *file bytes*, not the parsed records, because that
    is what an object-lock policy will be protecting and what a checksum on
    retrieval will compare against.
    """
    import datetime

    materialized = list(records)
    if report is None:
        report = verify(materialized)

    out = os.path.abspath(out_dir)
    os.makedirs(out, exist_ok=True)
    records_path = os.path.join(out, "records.jsonl")

    with open(records_path, "w", encoding="utf-8", newline="\n") as fh:
        for record in materialized:
            fh.write(json.dumps(record, sort_keys=True, ensure_ascii=False,
                                default=str) + "\n")

    with open(records_path, "rb") as fh:
        bundle_sha = hashlib.sha256(fh.read()).hexdigest()

    manifest = {
        "format": "agent-saga/worm-bundle",
        "format_version": 1,
        "exported_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source": source,
        "records": len(materialized),
        "chain_head": report.head,
        "intact": report.intact,
        "redacted": report.redacted,
        "attested_gaps": report.attested_gaps,
        "unchained": report.unchained,
        "breaks": [str(b) for b in report.breaks],
        "bundle_sha256": bundle_sha,
        "verification": {
            "hash": "sha256",
            "genesis": GENESIS,
            "content_digest": "sha256(salt_ascii || canonical_json(business_fields))",
            "record_hash": ("sha256(canonical_json({prev, seq, ts, event, cd})) "
                            "where cd is the content digest"),
            "canonical_json": "sort_keys=True, separators=(',',':'), ensure_ascii=False",
            "note": ("A record marked _redacted has had its payload and salt "
                     "destroyed by design; its _cd still binds it into the "
                     "chain, so its existence, position and type remain "
                     "provable while its contents are irrecoverable."),
        },
    }
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8",
              newline="\n") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return manifest


__all__ = [
    "GENESIS", "HASH_FIELD", "PREV_FIELD", "DIGEST_FIELD", "SALT_FIELD",
    "REDACTED_FIELD", "as_runs", "attested_seqs", "export_worm",
    "GAP_EVENT", "canonical", "content_digest", "record_hash", "business_fields",
    "stamp", "stamp_batch", "verify", "ChainReport", "ChainBreak",
    "redact_record", "redact_where", "gap_attestation", "digest_of",
]
