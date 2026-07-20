"""saga-recoveryd -- orphan detection and cross-process compensation.

A WAL nobody reads is an audit file. If a process is SIGKILLed after the
STEP_INTENT for a Stripe charge is fsynced, that charge is orphaned until an
independent process resolves it. This is that process.

Design commitments, in order of how much they matter to a regulated buyer:

  1. FAIL CLOSED. Anything the daemon cannot resolve with certainty is escalated
     to a human queue, never guessed at. An IRREVERSIBLE step anywhere in a
     dangling saga halts automated recovery for that saga entirely.
  2. LEASES, NOT PIDS. A PID tells you nothing -- they are reused within
     minutes. Only an expired lease proves the owner is gone.
  3. DETERMINISTIC TOKENS. Two daemons racing on the same WAL derive identical
     recovery tokens, so the second one sees the first one's journal entry and
     declines. Double-refunds are structurally impossible, not merely unlikely.
"""

from __future__ import annotations

import asyncio
import enum
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from .registry import resolve
from .semantics import ActionSemantics

logger = logging.getLogger("agent_saga.recovery")


class Resolution(enum.Enum):
    RECOVERED = "RECOVERED"
    """Every dangling effect was compensated."""
    NEEDS_HUMAN = "NEEDS_HUMAN"
    """Halted deliberately. An operator must look at this."""
    NOTHING_TO_DO = "NOTHING_TO_DO"
    SKIPPED_ACTIVE = "SKIPPED_ACTIVE"
    """Lease still valid -- the owning process is alive and will handle it."""
    SKIPPED_CLAIMED = "SKIPPED_CLAIMED"
    """Another daemon holds the claim."""


@dataclass
class DanglingStep:
    step_id: str
    tool: str
    semantics: ActionSemantics
    state: str                      # COMMITTED | UNKNOWN | INTENT_LOGGED
    compensation: Optional[dict]    # WAL descriptor, not a callable
    order: int

    @property
    def needs_compensation(self) -> bool:
        # INTENT_LOGGED without a terminal record is the nastiest case: the
        # process died somewhere around the network call. We cannot know if the
        # effect landed, so we must assume it did.
        return self.state in ("COMMITTED", "UNKNOWN", "INTENT_LOGGED")


@dataclass
class DanglingSaga:
    saga_id: str
    steps: list[DanglingStep] = field(default_factory=list)
    completed: bool = False
    aborted: bool = False
    rollback_finished: bool = False
    last_lease: float = 0.0
    lease_ttl: float = 5.0
    pid: Optional[int] = None
    compensated_step_ids: set[str] = field(default_factory=set)

    def lease_expired(self, now: Optional[float] = None) -> bool:
        # Grace of 2x TTL: a GC pause or a stalled disk must not be mistaken
        # for a dead process. False positives here cause double-compensation.
        return (now or time.time()) > self.last_lease + (self.lease_ttl * 2)

    @property
    def resolved_in_process(self) -> bool:
        return self.completed or (self.aborted and self.rollback_finished)

    def pending(self) -> list[DanglingStep]:
        return [s for s in self.steps
                if s.needs_compensation and s.step_id not in self.compensated_step_ids]


def parse_wal(records: Iterable[dict]) -> dict[str, DanglingSaga]:
    """Fold a WAL event stream into per-saga state."""
    sagas: dict[str, DanglingSaga] = {}

    def get(sid: str) -> DanglingSaga:
        return sagas.setdefault(sid, DanglingSaga(saga_id=sid))

    for rec in sorted(records, key=lambda r: r.get("seq", 0)):
        sid = rec.get("saga_id")
        if not sid:
            continue
        ev = rec.get("event")
        saga = get(sid)

        if ev == "SAGA_START":
            saga.last_lease = rec.get("ts", 0.0)
            saga.lease_ttl = rec.get("lease_ttl", 5.0)
            saga.pid = rec.get("pid")
        elif ev == "SAGA_LEASE":
            saga.last_lease = max(saga.last_lease, rec.get("ts", 0.0))
        elif ev == "SAGA_COMPLETE":
            saga.completed = True
        elif ev == "SAGA_ABORTED":
            saga.aborted = True
        elif ev == "ROLLBACK_END":
            saga.rollback_finished = True
        elif ev in ("STEP_INTENT", "STEP_COMMITTED", "STEP_UNKNOWN"):
            step = _find(saga, rec["step_id"])
            if step is None:
                step = DanglingStep(
                    step_id=rec["step_id"], tool=rec.get("tool", "?"),
                    semantics=ActionSemantics(rec.get("semantics", "COMPENSABLE")),
                    state="INTENT_LOGGED", compensation=None, order=len(saga.steps),
                )
                saga.steps.append(step)
            if ev == "STEP_COMMITTED":
                step.state = "COMMITTED"
                step.compensation = rec.get("compensation")
            elif ev == "STEP_UNKNOWN":
                step.state = "UNKNOWN"
                step.compensation = rec.get("compensation")
        elif ev == "COMPENSATED":
            saga.compensated_step_ids.add(rec["step_id"])

    return sagas


def _find(saga: DanglingSaga, step_id: str) -> Optional[DanglingStep]:
    for s in saga.steps:
        if s.step_id == step_id:
            return s
    return None


def recovery_token(saga_id: str, step_id: str) -> str:
    """Deterministic across processes, machines, and restarts. Two daemons
    independently derive the same token for the same step -- that identity is
    what makes double-compensation impossible."""
    return hashlib.sha256(f"{saga_id}:{step_id}:compensate".encode()).hexdigest()[:32]


@dataclass
class RecoveryOutcome:
    saga_id: str
    resolution: Resolution
    compensated: list[str] = field(default_factory=list)
    escalated: list[str] = field(default_factory=list)
    reason: str = ""


class RecoveryDaemon:
    def __init__(
        self,
        wal_path: str | Path,
        *,
        journal_path: Optional[str | Path] = None,
        claims_dir: Optional[str | Path] = None,
        daemon_id: Optional[str] = None,
        dry_run: bool = False,
    ):
        self.wal_path = Path(wal_path)
        self.journal_path = Path(journal_path) if journal_path else self.wal_path.with_suffix(".recovery.jsonl")
        self.claims_dir = Path(claims_dir) if claims_dir else self.wal_path.parent / ".claims"
        self.daemon_id = daemon_id or f"{os.getpid()}-{os.urandom(4).hex()}"
        self.dry_run = dry_run
        """Enterprises will not point this at production until they have watched
        it narrate what it *would* do for a week. Make that the easy path."""

    # -- journal -----------------------------------------------------------

    def _journal(self, event: str, payload: dict) -> None:
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        rec = {"event": event, "ts": time.time(), "daemon_id": self.daemon_id, **payload}
        with open(self.journal_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _completed_tokens(self) -> set[str]:
        if not self.journal_path.exists():
            return set()
        done = set()
        with open(self.journal_path, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                rec = json.loads(line)
                if rec.get("event") == "RECOVERY_SUCCESS":
                    done.add(rec["token"])
        return done

    def _claim(self, saga_id: str) -> bool:
        """O_EXCL create is atomic on POSIX and Windows alike. This is the
        mutual exclusion; the journal is the audit trail."""
        self.claims_dir.mkdir(parents=True, exist_ok=True)
        path = self.claims_dir / f"{saga_id}.claim"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps({"daemon_id": self.daemon_id, "ts": time.time()}))
        return True

    def _release(self, saga_id: str) -> None:
        try:
            (self.claims_dir / f"{saga_id}.claim").unlink()
        except FileNotFoundError:
            pass

    # -- scanning ----------------------------------------------------------

    def scan(self) -> dict[str, DanglingSaga]:
        if not self.wal_path.exists():
            return {}
        with open(self.wal_path, encoding="utf-8") as fh:
            records = [json.loads(line) for line in fh if line.strip()]
        return parse_wal(records)

    def dangling(self) -> list[DanglingSaga]:
        return [s for s in self.scan().values()
                if not s.resolved_in_process and s.pending()]

    # -- resolution --------------------------------------------------------

    async def recover_all(self) -> list[RecoveryOutcome]:
        return [await self.recover(s) for s in self.dangling()]

    async def recover(self, saga: DanglingSaga) -> RecoveryOutcome:
        if not saga.lease_expired():
            return RecoveryOutcome(saga.saga_id, Resolution.SKIPPED_ACTIVE,
                                   reason="lease is still being renewed; owner is alive")

        pending = saga.pending()
        if not pending:
            return RecoveryOutcome(saga.saga_id, Resolution.NOTHING_TO_DO)

        # Case C: fail closed on anything irreversible, before touching anything.
        irreversible = [s for s in pending if s.semantics is ActionSemantics.IRREVERSIBLE]
        if irreversible:
            self._journal("RECOVERY_ESCALATED", {
                "saga_id": saga.saga_id,
                "reason": "irreversible step present in a dangling saga",
                "steps": [s.tool for s in irreversible]})
            return RecoveryOutcome(
                saga.saga_id, Resolution.NEEDS_HUMAN,
                escalated=[s.tool for s in irreversible],
                reason=("saga contains IRREVERSIBLE step(s); automated recovery halted. "
                        "A human must confirm what actually happened."))

        if not self._claim(saga.saga_id):
            return RecoveryOutcome(saga.saga_id, Resolution.SKIPPED_CLAIMED,
                                   reason="another daemon holds the claim")

        done_tokens = self._completed_tokens()
        compensated: list[str] = []
        try:
            # LIFO, same ordering guarantee as an in-process rollback.
            for step in sorted(pending, key=lambda s: s.order, reverse=True):
                token = recovery_token(saga.saga_id, step.step_id)
                if token in done_tokens:
                    continue

                desc = step.compensation
                if not desc or not desc.get("recoverable"):
                    reason = ("no compensation was recorded" if not desc
                              else "compensation is in-process only (closure or "
                                   "non-serializable kwargs)")
                    self._journal("RECOVERY_ESCALATED", {
                        "saga_id": saga.saga_id, "step_id": step.step_id,
                        "tool": step.tool, "reason": reason})
                    return RecoveryOutcome(
                        saga.saga_id, Resolution.NEEDS_HUMAN, compensated=compensated,
                        escalated=[step.tool],
                        reason=f"{step.tool}: {reason}; halted before earlier steps")

                handler = resolve(desc["handler"])
                if handler is None:
                    self._journal("RECOVERY_ESCALATED", {
                        "saga_id": saga.saga_id, "step_id": step.step_id,
                        "tool": step.tool, "handler": desc["handler"],
                        "reason": "handler not registered in this daemon"})
                    return RecoveryOutcome(
                        saga.saga_id, Resolution.NEEDS_HUMAN, compensated=compensated,
                        escalated=[step.tool],
                        reason=(f"handler {desc['handler']!r} is not registered in the "
                                f"daemon; it must import the same connectors as the agent"))

                if self.dry_run:
                    self._journal("RECOVERY_DRY_RUN", {
                        "saga_id": saga.saga_id, "step_id": step.step_id,
                        "tool": step.tool, "handler": desc["handler"],
                        "kwargs": desc.get("kwargs", {}), "token": token})
                    compensated.append(step.tool)
                    continue

                # Journal the attempt BEFORE acting -- same write-ahead rule the
                # agent follows. A daemon that crashes mid-compensation must
                # leave evidence it tried.
                self._journal("RECOVERY_ATTEMPT", {
                    "saga_id": saga.saga_id, "step_id": step.step_id, "tool": step.tool,
                    "handler": desc["handler"], "token": token,
                    "idempotency_key": desc.get("idempotency_key")})

                try:
                    await _call(handler, dict(desc.get("kwargs") or {}))
                except BaseException as exc:
                    self._journal("RECOVERY_FAILED", {
                        "saga_id": saga.saga_id, "step_id": step.step_id,
                        "tool": step.tool, "token": token, "error": repr(exc)})
                    return RecoveryOutcome(
                        saga.saga_id, Resolution.NEEDS_HUMAN, compensated=compensated,
                        escalated=[step.tool],
                        reason=f"compensation for {step.tool} failed: {exc!r}; halted")

                self._journal("RECOVERY_SUCCESS", {
                    "saga_id": saga.saga_id, "step_id": step.step_id,
                    "tool": step.tool, "token": token})
                compensated.append(step.tool)
        finally:
            self._release(saga.saga_id)

        return RecoveryOutcome(saga.saga_id, Resolution.RECOVERED, compensated=compensated)

    async def watch(self, interval: float = 5.0) -> None:
        while True:
            try:
                for outcome in await self.recover_all():
                    if outcome.resolution is Resolution.NEEDS_HUMAN:
                        logger.error("saga %s needs a human: %s",
                                     outcome.saga_id, outcome.reason)
                    elif outcome.resolution is Resolution.RECOVERED:
                        logger.info("saga %s recovered: %s",
                                    outcome.saga_id, ", ".join(outcome.compensated))
            except Exception:
                logger.exception("recovery sweep failed; continuing")
            await asyncio.sleep(interval)


async def _call(fn: Any, kwargs: dict) -> Any:
    import inspect

    if inspect.iscoroutinefunction(fn):
        return await fn(**kwargs)
    return await asyncio.to_thread(lambda: fn(**kwargs))


__all__ = ["RecoveryDaemon", "DanglingSaga", "DanglingStep", "Resolution",
           "RecoveryOutcome", "parse_wal", "recovery_token"]
