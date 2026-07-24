"""Offline-first WAL sync: deterministic, conflict-free merge across devices.

Two phones go offline in a basement. Each runs sagas and appends to its own WAL.
Hours later both reconnect. Whatever reconciles those logs has to be *boring*:
it must produce the same result on both devices, in either direction, however
many times it runs -- otherwise two users looking at the same marketplace
disagree about what happened, and the log stops being evidence.

A WAL is append-only and its records are immutable, which makes the merge a
grow-only set (G-Set) -- the simplest CRDT there is. Union is commutative,
associative and idempotent by construction. The work is in the three things a
naive union gets wrong:

* **Identity.** `seq` is per-device and collides across devices. Identity here is
  a hash of the record's *content*, so the same record synced twice dedupes, and
  two genuinely different records that share a `seq` do not.
* **Order.** The merged log needs one total order that both devices compute
  identically, without talking. `(ts, device, seq, identity)` is fully determined
  by the data, so it never depends on who merged first.
* **Divergence.** A saga advanced independently on two devices is not a merge
  conflict -- both sets of events really happened -- but it *is* an operational
  signal, so it is reported rather than hidden.

    merged, report = merge_wals({"phone-a": a_records, "phone-b": b_records})
    report.diverged_sagas      # touched on more than one device

The hash chain is per-device by construction, so a merged log is not one linear
chain. `verify_merged` checks each device's chain independently -- which is the
honest guarantee, and still detects tampering with any device's history.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

from .integrity import canonical

logger = logging.getLogger("agent_saga.mesh")

# Fields the merge itself adds. Excluded from identity so that stamping a record
# with its origin device does not change what record it *is* -- otherwise the
# same record synced from two peers would look like two records.
MERGE_META = ("_dev",)
DEVICE_FIELD = "_dev"


def record_identity(record: Mapping[str, Any]) -> str:
    """Content-derived identity. Two devices holding the same record agree."""
    payload = {k: v for k, v in record.items() if k not in MERGE_META}
    return hashlib.sha256(canonical(payload)).hexdigest()


def _order_key(record: Mapping[str, Any], identity: str) -> tuple:
    """A total order both devices compute identically from the data alone."""
    ts = record.get("ts")
    ts = float(ts) if isinstance(ts, (int, float)) else 0.0
    seq = record.get("seq")
    seq = int(seq) if isinstance(seq, int) else 0
    return (ts, str(record.get(DEVICE_FIELD) or ""), seq, identity)


@dataclass
class MergeReport:
    total: int = 0
    added: int = 0
    duplicates: int = 0
    devices: list[str] = field(default_factory=list)
    diverged_sagas: list[str] = field(default_factory=list)
    sagas: int = 0

    def summary(self) -> str:
        div = (f", {len(self.diverged_sagas)} saga(s) advanced on >1 device"
               if self.diverged_sagas else "")
        return (f"merged {self.total} record(s) from {len(self.devices)} device(s): "
                f"{self.added} new, {self.duplicates} duplicate{div}")


def merge_wals(sources: Any, *, stamp_device: bool = True) -> tuple[list[dict], MergeReport]:
    """Merge WAL segments from several devices into one deterministic log.

    `sources` is either ``{device_id: records}`` or a sequence of record
    sequences (auto-named ``device-0``, ``device-1``, ...).

    Guarantees, each covered by a property test:
      * **idempotent** -- merging a log with itself changes nothing
      * **commutative** -- merge(A, B) == merge(B, A)
      * **associative** -- merge(merge(A, B), C) == merge(A, merge(B, C))
    """
    if isinstance(sources, Mapping):
        items = list(sources.items())
    else:
        items = [(f"device-{i}", recs) for i, recs in enumerate(sources)]

    by_identity: dict[str, dict] = {}
    device_of: dict[str, str] = {}
    duplicates = 0
    devices: set[str] = set()

    for device_id, records in items:
        devices.add(device_id)
        for rec in records or []:
            # A record may already carry an origin from an earlier merge; that
            # origin wins, so re-syncing through a third peer is still stable.
            origin = str(rec.get(DEVICE_FIELD) or device_id)
            ident = record_identity(rec)

            if ident in by_identity:
                duplicates += 1
                # Deterministic tie-break: the lexicographically smallest origin
                # wins, so both peers agree without coordinating.
                if origin < device_of[ident]:
                    device_of[ident] = origin
                continue
            by_identity[ident] = dict(rec)
            device_of[ident] = origin

    merged: list[dict] = []
    # Divergence is computed over DEDUPED records: a record merely synced to both
    # devices is one record, not two devices advancing the saga. Counting input
    # occurrences instead would flag every already-synced saga as diverged.
    saga_devices: dict[str, set[str]] = {}
    for ident, rec in by_identity.items():
        origin = device_of[ident]
        if stamp_device:
            rec[DEVICE_FIELD] = origin
        saga_id = rec.get("saga_id")
        if saga_id:
            saga_devices.setdefault(str(saga_id), set()).add(origin)
        merged.append(rec)

    merged.sort(key=lambda r: _order_key(r, record_identity(r)))

    diverged = sorted(sid for sid, devs in saga_devices.items() if len(devs) > 1)
    report = MergeReport(
        total=len(merged),
        added=len(merged),
        duplicates=duplicates,
        devices=sorted(devices),
        diverged_sagas=diverged,
        sagas=len(saga_devices),
    )
    if diverged:
        logger.info("merge: %d saga(s) advanced on more than one device: %s",
                    len(diverged), ", ".join(diverged[:5]))
    return merged, report


def split_by_device(records: Sequence[Mapping[str, Any]]) -> dict[str, list[dict]]:
    """Group a merged log back into its per-device segments."""
    out: dict[str, list[dict]] = {}
    for rec in records:
        out.setdefault(str(rec.get(DEVICE_FIELD) or ""), []).append(dict(rec))
    return out


def verify_merged(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Verify each device's hash chain independently.

    A merged log interleaves several chains, so it is not one linear chain -- and
    claiming otherwise would be the kind of overstatement that gets a compliance
    story thrown out. Each device's own history is still fully checkable, which
    is what actually detects tampering.
    """
    from .integrity import verify as verify_chain

    results: dict[str, Any] = {"devices": {}, "intact": True}
    for device_id, recs in split_by_device(records).items():
        # Strip merge metadata before verifying: the chain was computed over the
        # record as originally written.
        original = [{k: v for k, v in r.items() if k not in MERGE_META} for r in recs]
        report = verify_chain(original)
        results["devices"][device_id] = {
            "records": len(original),
            "intact": report.intact,
            "summary": report.summary(),
        }
        if not report.intact:
            results["intact"] = False
    return results


__all__ = [
    "merge_wals", "record_identity", "verify_merged", "split_by_device",
    "MergeReport", "MERGE_META", "DEVICE_FIELD",
]
