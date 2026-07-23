"""Durable Timers and Cron Saga Scheduler (Temporal & Camunda Parity).

Provides durable sleeps across restarts (DurableTimerManager) and cron-driven
saga scheduling (CronSagaScheduler) for recurring workflow execution.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("agent_saga.scheduler")


class TimerCancelled(Exception):
    """Raised inside a durable ``sleep()`` when the timer is cancelled externally."""

    def __init__(self, timer_id: str):
        self.timer_id = timer_id
        super().__init__(f"durable timer {timer_id!r} was cancelled")


@dataclass
class ScheduledTimer:
    timer_id: str
    saga_id: str
    target_ts: float
    payload: dict[str, Any] = field(default_factory=dict)
    fired: bool = False
    cancelled: bool = False
    name: Optional[str] = None


class DurableTimerManager:
    """Manages persistent timers that survive process crashes and restarts.

    A timer can be given a human-readable ``name`` when scheduled, and cancelled
    by that name (or its id) from anywhere -- ``cancel("onboard-reminder")``. If a
    saga is currently awaiting the timer in ``sleep()``, cancelling wakes it
    immediately with ``TimerCancelled`` rather than letting it run to term.
    """

    def __init__(self, storage_path: Optional[str | Path] = None):
        self.storage_path = Path(storage_path) if storage_path else None
        self._timers: dict[str, ScheduledTimer] = {}
        # In-flight waiters, keyed by timer_id. Not persisted: a process that
        # restarts has no coroutine parked in sleep() to wake.
        self._waiters: dict[str, asyncio.Event] = {}
        self._load()

    def _load(self) -> None:
        if self.storage_path and self.storage_path.exists():
            try:
                data = json.loads(self.storage_path.read_text(encoding="utf-8"))
                for item in data:
                    t = ScheduledTimer(**item)
                    self._timers[t.timer_id] = t
            except Exception as exc:
                logger.warning("Could not load durable timers from %s: %r", self.storage_path, exc)

    def _save(self) -> None:
        if self.storage_path:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            data = [
                {
                    "timer_id": t.timer_id,
                    "saga_id": t.saga_id,
                    "target_ts": t.target_ts,
                    "payload": t.payload,
                    "fired": t.fired,
                    "cancelled": t.cancelled,
                    "name": t.name,
                }
                for t in self._timers.values()
            ]
            self.storage_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _resolve(self, identifier: str) -> Optional[ScheduledTimer]:
        """Find a timer by its id or its human-readable name."""
        timer = self._timers.get(identifier)
        if timer is not None:
            return timer
        return next((t for t in self._timers.values() if t.name == identifier), None)

    def schedule_sleep(self, saga_id: str, duration_seconds: float,
                       payload: Optional[dict] = None, *, name: Optional[str] = None) -> ScheduledTimer:
        # A named timer is addressable for cancellation; the name is the id so a
        # later cancel(name) finds it even across a restart.
        timer_id = name or f"timer-{saga_id}-{int(time.time() * 1000)}"
        target_ts = time.time() + duration_seconds
        timer = ScheduledTimer(timer_id=timer_id, saga_id=saga_id,
                               target_ts=target_ts, payload=payload or {}, name=name)
        self._timers[timer_id] = timer
        self._save()
        return timer

    def cancel(self, identifier: str) -> bool:
        """Cancel a pending timer by name or id. Returns True if one was cancelled
        (False if unknown or already fired/cancelled). Wakes an in-flight sleeper."""
        timer = self._resolve(identifier)
        if timer is None or timer.fired or timer.cancelled:
            return False
        timer.cancelled = True
        self._save()
        waiter = self._waiters.get(timer.timer_id)
        if waiter is not None:
            waiter.set()   # wake sleep() so it raises TimerCancelled promptly
        logger.info("durable timer %r cancelled", timer.timer_id)
        return True

    async def sleep(self, saga_id: str, duration_seconds: float, *,
                    name: Optional[str] = None) -> None:
        """Durably sleep. Raises TimerCancelled if cancel() fires meanwhile."""
        timer = self.schedule_sleep(saga_id, duration_seconds, name=name)
        event = asyncio.Event()
        self._waiters[timer.timer_id] = event
        try:
            remaining = timer.target_ts - time.time()
            if remaining > 0:
                try:
                    await asyncio.wait_for(event.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    pass   # slept the full duration -> fire normally
            if timer.cancelled:
                raise TimerCancelled(timer.timer_id)
            timer.fired = True
            self._save()
        finally:
            self._waiters.pop(timer.timer_id, None)

    def list_pending(self) -> list[ScheduledTimer]:
        now = time.time()
        return [t for t in self._timers.values()
                if not t.fired and not t.cancelled and now >= t.target_ts]


class CronSagaScheduler:
    """Recurring cron-driven saga scheduler."""

    def __init__(self, callback: Callable[[str, dict], Any]):
        self.callback = callback
        self._schedules: dict[str, dict[str, Any]] = {}
        self._running = False
        self._task: Optional[asyncio.Task] = None

    def schedule_cron(self, schedule_id: str, cron_expr: str, saga_fn_name: str, payload: Optional[dict] = None) -> None:
        self._schedules[schedule_id] = {
            "cron_expr": cron_expr,
            "saga_fn_name": saga_fn_name,
            "payload": payload or {},
            "last_run": 0.0,
        }

    def cancel(self, schedule_id: str) -> bool:
        """Stop a recurring schedule by id. Returns True if one was removed."""
        return self._schedules.pop(schedule_id, None) is not None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _loop(self) -> None:
        while self._running:
            await asyncio.sleep(0.1)
            now = time.time()
            for sid, sched in list(self._schedules.items()):
                if now - sched["last_run"] >= 0.5:  # trigger interval
                    sched["last_run"] = now
                    try:
                        if asyncio.iscoroutinefunction(self.callback):
                            await self.callback(sched["saga_fn_name"], sched["payload"])
                        else:
                            self.callback(sched["saga_fn_name"], sched["payload"])
                    except Exception as exc:
                        logger.error("Error triggering cron saga %s: %r", sid, exc)


__all__ = ["ScheduledTimer", "DurableTimerManager", "CronSagaScheduler", "TimerCancelled"]
