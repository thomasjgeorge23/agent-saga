"""Snapshot-store garbage collection.

A durable snapshot exists only to undo a saga. Once the saga is *resolved* --
committed (undo never needed) or rolled back (undo already done) -- its snapshot
is dead weight. Without a sweep the store grows without bound; this is the sweep.

The whole design is built around one failure it must never cause: deleting a
snapshot a rollback still needs. Three guards enforce that:

  1. A snapshot referenced by an UNRESOLVED saga is always kept. If the owning
     saga is still running, or crashed and not yet recovered, its undo data
     stays put.

  2. A snapshot referenced by a saga that ESCALATED to a human is kept. The
     operator resolving it by hand may need to restore from it.

  3. A grace period. Even a resolved saga's snapshots are kept until its last
     WAL activity is older than `grace_seconds` (default 1h), so the sweep never
     races a saga that resolved a moment ago or a daemon mid-recovery -- the same
     caution the recovery daemon applies with its lease.

Resolution is read from the WAL (SAGA_COMPLETE, or SAGA_ABORTED + ROLLBACK_END)
and, optionally, from the recovery daemon's journal (a saga whose steps the
daemon compensated). Anything the sweep is unsure about, it keeps.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .durable import SnapshotStore, get_snapshot_store

logger = logging.getLogger("agent_saga.gc")


@dataclass
class GCReport:
    scanned: int = 0
    deleted: list[str] = field(default_factory=list)
    kept_active: int = 0        # referenced by an unresolved / escalated saga
    kept_young: int = 0         # resolved, but within the grace period
    kept_unreferenced: int = 0  # no WAL reference and too young / age unknown

    def summary(self) -> str:
        return (f"GC: {len(self.deleted)} deleted, {self.kept_active} active-kept, "
                f"{self.kept_young} within-grace, {self.kept_unreferenced} "
                f"unreferenced-kept (of {self.scanned} scanned)")


@dataclass
class _SagaLife:
    resolved: bool = False
    escalated: bool = False
    last_ts: float = 0.0
    snapshot_ids: set = field(default_factory=set)


class SnapshotGC:
    def __init__(
        self,
        wal_path: str | Path,
        store: Optional[SnapshotStore] = None,
        *,
        recovery_journal: Optional[str | Path] = None,
        grace_seconds: float = 3600.0,
        dry_run: bool = False,
    ):
        self.wal_path = Path(wal_path)
        self.store = store or get_snapshot_store()
        self.recovery_journal = Path(recovery_journal) if recovery_journal else None
        self.grace_seconds = grace_seconds
        self.dry_run = dry_run

    # -- read saga lifecycles from the WAL (and optional recovery journal) ---

    def _sagas(self) -> dict[str, _SagaLife]:
        from .ui.reader import iter_records  # truncation-tolerant

        sagas: dict[str, _SagaLife] = {}

        def life(sid: str) -> _SagaLife:
            return sagas.setdefault(sid, _SagaLife())

        aborted: dict[str, bool] = {}
        rolled_back: dict[str, bool] = {}

        for rec in iter_records(self.wal_path):
            sid = rec["saga_id"]
            L = life(sid)
            ts = rec.get("ts")
            if isinstance(ts, (int, float)):
                L.last_ts = max(L.last_ts, ts)
            ev = rec.get("event")

            if ev == "SAGA_COMPLETE":
                L.resolved = True
            elif ev == "SAGA_ABORTED":
                aborted[sid] = True
            elif ev == "ROLLBACK_END":
                rolled_back[sid] = True
            elif ev in ("STEP_COMMITTED", "STEP_UNKNOWN"):
                comp = rec.get("compensation") or {}
                sid_snap = (comp.get("kwargs") or {}).get("snapshot_id")
                if sid_snap:
                    L.snapshot_ids.add(sid_snap)

        # A saga that aborted AND finished its rollback is resolved in-process.
        for sid in sagas:
            if aborted.get(sid) and rolled_back.get(sid):
                sagas[sid].resolved = True

        self._apply_recovery_journal(sagas)
        return sagas

    def _apply_recovery_journal(self, sagas: dict[str, _SagaLife]) -> None:
        """A crashed saga has no SAGA_COMPLETE/ABORTED in the WAL; only the
        daemon's journal knows it was resolved. A saga the daemon escalated must
        be kept for the human, so escalation wins over success."""
        if not self.recovery_journal or not self.recovery_journal.exists():
            return
        recovered: dict[str, bool] = {}
        escalated: set = set()
        with open(self.recovery_journal, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                sid = rec.get("saga_id")
                if not sid:
                    continue
                ev = rec.get("event")
                if ev == "RECOVERY_SUCCESS":
                    recovered[sid] = True
                elif ev in ("RECOVERY_ESCALATED", "RECOVERY_FAILED"):
                    escalated.add(sid)
                ts = rec.get("ts")
                if sid in sagas and isinstance(ts, (int, float)):
                    sagas[sid].last_ts = max(sagas[sid].last_ts, ts)

        for sid, L in sagas.items():
            if sid in escalated:
                L.escalated = True
            elif recovered.get(sid):
                L.resolved = True

    # -- the sweep ----------------------------------------------------------

    def collect(self, *, now: Optional[float] = None) -> GCReport:
        now = time.time() if now is None else now
        sagas = self._sagas()

        # Reverse index: snapshot_id -> owning saga life.
        owner: dict[str, _SagaLife] = {}
        for L in sagas.values():
            for snap in L.snapshot_ids:
                owner[snap] = L

        report = GCReport()
        for snap in self.store.list_ids():
            report.scanned += 1
            L = owner.get(snap)

            if L is None:
                # No saga references it. Could be a pre-commit crash orphan, or a
                # reference in a WAL we are not looking at. Only reap if we can
                # prove it is old; otherwise keep.
                age = self.store.age_seconds(snap)
                if age is not None and age > self.grace_seconds:
                    self._delete(snap, report, reason="unreferenced+old")
                else:
                    report.kept_unreferenced += 1
                continue

            if not L.resolved or L.escalated:
                report.kept_active += 1
                continue

            if (now - L.last_ts) <= self.grace_seconds:
                report.kept_young += 1
                continue

            self._delete(snap, report, reason="resolved+aged-out")

        if report.deleted or report.scanned:
            logger.info("%s%s", "[dry-run] " if self.dry_run else "", report.summary())
        return report

    def _delete(self, snap: str, report: GCReport, *, reason: str) -> None:
        if not self.dry_run:
            self.store.delete(snap)
        report.deleted.append(snap)
        logger.debug("%sreaped snapshot %s (%s)",
                     "[dry-run] " if self.dry_run else "", snap, reason)


__all__ = ["SnapshotGC", "GCReport"]
