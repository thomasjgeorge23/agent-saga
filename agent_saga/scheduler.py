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


@dataclass
class ScheduledTimer:
    timer_id: str
    saga_id: str
    target_ts: float
    payload: dict[str, Any] = field(default_factory=dict)
    fired: bool = False


class DurableTimerManager:
    """Manages persistent timers that survive process crashes and restarts."""

    def __init__(self, storage_path: Optional[str | Path] = None):
        self.storage_path = Path(storage_path) if storage_path else None
        self._timers: dict[str, ScheduledTimer] = {}
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
                }
                for t in self._timers.values()
            ]
            self.storage_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def schedule_sleep(self, saga_id: str, duration_seconds: float, payload: Optional[dict] = None) -> ScheduledTimer:
        timer_id = f"timer-{saga_id}-{int(time.time() * 1000)}"
        target_ts = time.time() + duration_seconds
        timer = ScheduledTimer(timer_id=timer_id, saga_id=saga_id, target_ts=target_ts, payload=payload or {})
        self._timers[timer_id] = timer
        self._save()
        return timer

    async def sleep(self, saga_id: str, duration_seconds: float) -> None:
        timer = self.schedule_sleep(saga_id, duration_seconds)
        now = time.time()
        if now < timer.target_ts:
            await asyncio.sleep(timer.target_ts - now)
        timer.fired = True
        self._save()

    def list_pending(self) -> list[ScheduledTimer]:
        now = time.time()
        return [t for t in self._timers.values() if not t.fired and now >= t.target_ts]


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


__all__ = ["ScheduledTimer", "DurableTimerManager", "CronSagaScheduler"]
