"""WAL reader for the time-travel debugger.

Pure parsing, no HTTP -- so it is unit-testable on its own and a different
transport (FastAPI, a notebook, a CLI dump) could sit on top unchanged.

Three properties the brief demands and this honors:

  * Streaming. The file is read one line at a time; only compact per-saga
    summaries are held. A multi-gigabyte WAL never lands in RAM whole. (A
    production build would keep a byte-offset index per saga to make the detail
    view O(one saga) instead of O(scan); noted, not built.)

  * Truncation-tolerant. A crash mid-write leaves a partial final line. Any line
    that fails to parse is counted and skipped, never fatal -- a debugger that
    itself crashes on a crashed WAL is worse than useless.

  * Secret-scrubbing. Credentials are already kept out of the WAL by design
    (connectors log references, not values). This is the second line of defense:
    forward kwargs come from arbitrary agent code, so anything that *looks* like
    a secret is redacted before it reaches a browser.

Faithfulness note: the WAL records that a saga aborted and rollback began, but
NOT the Python exception that triggered it (that lives in the raised
SagaAborted, never written to disk). The timeline therefore shows an honest
"aborted -> rollback" marker rather than inventing a specific failing step or a
cause it cannot know.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

from ..connectors._secrets import _PATTERNS, _SUSPICIOUS_KEYS

REDACTED = "« redacted »"
_MAX_STR = 2000  # keep a runaway kwarg from bloating the API response


# ---------------------------------------------------------------------------
# Secret scrubbing
# ---------------------------------------------------------------------------

def scrub(value: Any, *, key: Optional[str] = None) -> Any:
    """Redact anything that looks like a credential, by key name or by value.

    Mirrors the authoring-time `assert_no_secrets` guard, but here it never
    raises -- historical data may predate that guard, so we defend rather than
    reject.
    """
    if key is not None and _SUSPICIOUS_KEYS.search(key) and not key.endswith(
        ("_ref", "_reference", "_name")
    ):
        return REDACTED
    if isinstance(value, str):
        for pattern, _label in _PATTERNS:
            if pattern.match(value):
                return REDACTED
        return value if len(value) <= _MAX_STR else value[:_MAX_STR] + "…"
    if isinstance(value, dict):
        return {k: scrub(v, key=k) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Streaming record iteration
# ---------------------------------------------------------------------------

@dataclass
class ParseStats:
    total_lines: int = 0
    corrupt_lines: int = 0


def iter_records(path: Path, stats: Optional[ParseStats] = None) -> Iterator[dict]:
    """Yield one parsed JSON object per line, skipping unparseable lines.

    Only a partial final line is expected in practice, but any bad line is
    tolerated -- the alternative is a viewer that dies on the exact corruption
    it exists to investigate.
    """
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            if stats is not None:
                stats.total_lines += 1
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                if stats is not None:
                    stats.corrupt_lines += 1
                continue
            if isinstance(rec, dict) and rec.get("saga_id"):
                yield rec


# ---------------------------------------------------------------------------
# Status derivation
# ---------------------------------------------------------------------------

# Saga-level, matching the brief's vocabulary plus one the brief omits.
SUCCESS = "SUCCESS"          # SAGA_COMPLETE
ROLLED_BACK = "ROLLED_BACK"  # aborted, rollback clean
FAILED = "FAILED"            # aborted, rollback left orphans/failures
IN_PROGRESS = "IN_PROGRESS"  # no terminal record (running, or crashed)

# Step-level display status the UI colors on.
STEP_COMMITTED = "COMMITTED"
STEP_COMPENSATED = "COMPENSATED"
STEP_COMPENSATION_FAILED = "COMPENSATION_FAILED"
STEP_ORPHANED = "ORPHANED"
STEP_UNKNOWN = "UNKNOWN"
STEP_GATED = "GATED"  # intent written, never committed (blocked or crashed pre-commit)


@dataclass
class _StepAcc:
    step_id: str
    tool: str = "?"
    semantics: str = "COMPENSABLE"
    order: int = 0
    intent_ts: Optional[float] = None
    committed_ts: Optional[float] = None
    status: str = STEP_GATED
    intent_kwargs: dict = field(default_factory=dict)
    compensation: Optional[dict] = None
    error: Optional[str] = None
    idempotency_key: Optional[str] = None

    def latency_ms(self) -> Optional[float]:
        if self.intent_ts is not None and self.committed_ts is not None:
            return round((self.committed_ts - self.intent_ts) * 1000, 3)
        return None


@dataclass
class _SagaAcc:
    saga_id: str
    first_ts: Optional[float] = None
    last_ts: Optional[float] = None
    pid: Optional[int] = None
    completed: bool = False
    aborted: bool = False
    cause_type: Optional[str] = None
    cause: Optional[str] = None
    rollback_started: bool = False
    rollback_clean: Optional[bool] = None
    rollback_summary: dict = field(default_factory=dict)
    steps: dict = field(default_factory=dict)  # step_id -> _StepAcc
    _order: int = 0

    def touch(self, ts: Optional[float]) -> None:
        if ts is None:
            return
        self.first_ts = ts if self.first_ts is None else min(self.first_ts, ts)
        self.last_ts = ts if self.last_ts is None else max(self.last_ts, ts)

    def step(self, step_id: str) -> _StepAcc:
        acc = self.steps.get(step_id)
        if acc is None:
            acc = _StepAcc(step_id=step_id, order=self._order)
            self._order += 1
            self.steps[step_id] = acc
        return acc

    @property
    def status(self) -> str:
        if self.completed:
            return SUCCESS
        if self.aborted:
            return ROLLED_BACK if self.rollback_clean else FAILED
        return IN_PROGRESS

    def apply(self, rec: dict) -> None:
        ev = rec.get("event")
        ts = rec.get("ts")
        self.touch(ts)
        sid = rec.get("step_id")

        if ev == "SAGA_START":
            self.pid = rec.get("pid")
        elif ev == "SAGA_COMPLETE":
            self.completed = True
        elif ev == "SAGA_ABORTED":
            self.aborted = True
        elif ev == "SAGA_ABORT_CAUSE":
            self.cause_type = rec.get("cause_type")
            self.cause = rec.get("cause")
        elif ev == "ROLLBACK_START":
            self.rollback_started = True
        elif ev == "ROLLBACK_END":
            self.rollback_clean = rec.get("clean")
            self.rollback_summary = {
                k: rec.get(k) for k in
                ("compensated", "failed", "orphaned", "unresolved", "halted")
            }
        elif ev == "STEP_INTENT" and sid:
            s = self.step(sid)
            s.tool = rec.get("tool", s.tool)
            s.semantics = rec.get("semantics", s.semantics)
            s.intent_ts = ts
            s.intent_kwargs = rec.get("kwargs", {}) or {}
        elif ev == "STEP_COMMITTED" and sid:
            s = self.step(sid)
            s.tool = rec.get("tool", s.tool)
            s.semantics = rec.get("semantics", s.semantics)
            s.committed_ts = ts
            s.status = STEP_COMMITTED
            s.compensation = rec.get("compensation")
        elif ev == "STEP_UNKNOWN" and sid:
            s = self.step(sid)
            s.tool = rec.get("tool", s.tool)
            s.semantics = rec.get("semantics", s.semantics)
            s.status = STEP_UNKNOWN
            s.error = rec.get("error")
            s.compensation = rec.get("compensation")
        elif ev == "STEP_ORPHANED" and sid:
            self.step(sid).status = STEP_ORPHANED
        elif ev == "COMPENSATED" and sid:
            s = self.step(sid)
            s.status = STEP_COMPENSATED
            s.idempotency_key = rec.get("idempotency_key")
        elif ev == "COMPENSATION_FAILED" and sid:
            s = self.step(sid)
            s.status = STEP_COMPENSATION_FAILED
            s.error = rec.get("error")
            s.idempotency_key = rec.get("idempotency_key")

    # -- projections -----------------------------------------------------

    def summary(self) -> dict:
        span_ms = None
        if self.first_ts is not None and self.last_ts is not None:
            span_ms = round((self.last_ts - self.first_ts) * 1000, 3)
        committed = [s for s in self.steps.values() if s.status != STEP_GATED]
        return {
            "saga_id": self.saga_id,
            "started_at": self.first_ts,
            "ended_at": self.last_ts,
            "status": self.status,
            "step_count": len(committed),
            "total_latency_ms": span_ms,
            "pid": self.pid,
        }

    def detail(self) -> dict:
        steps = []
        for s in sorted(self.steps.values(), key=lambda x: x.order):
            comp = None
            if s.compensation:
                comp = {
                    "handler": s.compensation.get("handler"),
                    "recoverable": s.compensation.get("recoverable"),
                    "description": s.compensation.get("description"),
                    "idempotency_key": s.compensation.get("idempotency_key"),
                    "kwargs": scrub(s.compensation.get("kwargs", {})),
                }
            steps.append({
                "step_id": s.step_id,
                "tool": s.tool,
                "semantics": s.semantics,
                "status": s.status,
                "order": s.order,
                "intent_ts": s.intent_ts,
                "committed_ts": s.committed_ts,
                "latency_ms": s.latency_ms(),
                "forward_kwargs": scrub(s.intent_kwargs),
                "compensation": comp,
                "error": s.error,
                "idempotency_key": s.idempotency_key,
            })
        d = self.summary()
        d.update({
            "rollback_started": self.rollback_started,
            "rollback_clean": self.rollback_clean,
            "rollback_summary": self.rollback_summary,
            # The triggering exception, when the saga ran through a boundary
            # that recorded it. scrub() catches a message that is itself a bare
            # secret; it cannot catch one embedded mid-sentence.
            "abort_cause": (
                {"type": self.cause_type, "message": scrub(self.cause)}
                if self.cause_type else None
            ),
            "steps": steps,
        })
        return d


class SagaWALReader:
    """Read-only view over a WAL file. Re-reads on each call so a live,
    still-growing file is reflected without a restart."""

    def __init__(self, wal_path: str | Path):
        self.wal_path = Path(wal_path)

    def meta(self) -> dict:
        stats = ParseStats()
        # Cheap existence/size; corruption count comes from a real pass on demand.
        exists = self.wal_path.exists()
        size = self.wal_path.stat().st_size if exists else 0
        return {
            "wal_path": str(self.wal_path.resolve()) if exists else str(self.wal_path),
            "exists": exists,
            "size_bytes": size,
        }

    def _accumulate(self, only: Optional[str] = None) -> tuple[dict, ParseStats]:
        stats = ParseStats()
        sagas: dict[str, _SagaAcc] = {}
        for rec in iter_records(self.wal_path, stats):
            sid = rec["saga_id"]
            if only is not None and sid != only:
                continue
            acc = sagas.get(sid)
            if acc is None:
                acc = _SagaAcc(saga_id=sid)
                sagas[sid] = acc
            acc.apply(rec)
        return sagas, stats

    def list_sagas(self, *, limit: int = 500) -> dict:
        sagas, stats = self._accumulate()
        summaries = sorted(
            (a.summary() for a in sagas.values()),
            key=lambda s: (s["started_at"] or 0),
            reverse=True,
        )
        return {
            "sagas": summaries[:limit],
            "total": len(summaries),
            "corrupt_lines": stats.corrupt_lines,
        }

    def get_saga(self, saga_id: str) -> Optional[dict]:
        sagas, _ = self._accumulate(only=saga_id)
        acc = sagas.get(saga_id)
        return acc.detail() if acc else None


__all__ = ["SagaWALReader", "scrub", "iter_records", "ParseStats"]
