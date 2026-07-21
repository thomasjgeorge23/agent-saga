"""Thread-pool isolation and instrumentation.

Sync work in this engine falls into two classes with completely different
availability requirements, and asyncio's default executor merges them:

  * TOOL WORK -- a blocking connector call or a compensation handler. Arbitrary
    duration, arbitrary count, entirely outside our control.
  * WAL FLUSHING -- one short fsync at a time, on the critical path of every
    durable step in the process.

`asyncio.to_thread` puts both on the same pool, capped at min(32, cpu+4). A burst
of slow tool calls therefore saturates it, the WAL flusher cannot get a thread,
and every `barrier()` in the process blocks -- so ten slow Salesforce calls stall
*every* saga, including ones touching nothing but Postgres. That is head-of-line
blocking on a shared resource, and it is an availability bug, not a tuning knob.

So the two are separated:

  * each `AsyncWAL` owns a private single-thread executor (it is a single writer;
    one thread is exactly right), which nothing else can ever occupy;
  * tool work runs on a bounded, instrumented pool sized for I/O concurrency and
    tunable at runtime, which reports its own saturation instead of silently
    queueing.

Both propagate `contextvars` into the worker thread, so correlation ids (and any
caller context) survive the hop -- `asyncio.to_thread` does this and a naive
`run_in_executor` does not, which would have silently dropped `saga_id` from
every log line emitted inside a sync connector.
"""

from __future__ import annotations

import asyncio
import contextvars
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

_DEFAULT_MAX_WORKERS = int(os.environ.get("AGENT_SAGA_TOOL_WORKERS", "0")) or max(
    32, min(256, (os.cpu_count() or 4) * 16)
)
"""Sized for I/O-bound work, not CPU-bound. Compensations are almost always a
network round trip, so threads spend their lives blocked and a pool far wider
than the core count is correct. Override with AGENT_SAGA_TOOL_WORKERS or
configure_tool_executor()."""


class ExecutorStats:
    """Live counters. `saturated` is the number that matters: it counts calls
    that arrived with every worker already busy, which is the early warning that
    the pool is now adding latency to compensations."""

    __slots__ = ("submitted", "completed", "in_flight", "peak_in_flight",
                 "saturated", "total_queue_wait_ns", "max_queue_wait_ns")

    def __init__(self) -> None:
        self.submitted = 0
        self.completed = 0
        self.in_flight = 0
        self.peak_in_flight = 0
        self.saturated = 0
        self.total_queue_wait_ns = 0
        self.max_queue_wait_ns = 0

    def as_dict(self, max_workers: int) -> dict:
        avg_wait_ms = (self.total_queue_wait_ns / self.completed / 1e6
                       if self.completed else 0.0)
        return {
            "max_workers": max_workers,
            "submitted": self.submitted,
            "completed": self.completed,
            "in_flight": self.in_flight,
            "peak_in_flight": self.peak_in_flight,
            "saturated": self.saturated,
            "avg_queue_wait_ms": round(avg_wait_ms, 4),
            "max_queue_wait_ms": round(self.max_queue_wait_ns / 1e6, 4),
            "utilization": round(self.peak_in_flight / max_workers, 3) if max_workers else 0.0,
        }


class BoundedExecutor:
    """A named thread pool that reports its own saturation.

    Deliberately not a semaphore-gated queue: back-pressuring the *caller* would
    turn pool exhaustion into stalled agent coroutines, which is the failure we
    are trying to remove. Instead the pool queues, measures the queue wait, and
    surfaces it, so an operator can widen the pool on evidence.
    """

    def __init__(self, *, max_workers: int = _DEFAULT_MAX_WORKERS,
                 name: str = "agent-saga-tool"):
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        self.max_workers = max_workers
        self.name = name
        self._pool = ThreadPoolExecutor(max_workers=max_workers,
                                        thread_name_prefix=name)
        self._lock = threading.Lock()
        self.stats = ExecutorStats()

    async def run(self, fn: Callable[..., Any], kwargs: Optional[dict] = None) -> Any:
        """Run a blocking callable on this pool, preserving contextvars."""
        kwargs = kwargs or {}
        loop = asyncio.get_running_loop()
        # Snapshot the caller's context so correlation ids reach the thread.
        ctx = contextvars.copy_context()
        queued_at = time.perf_counter_ns()

        with self._lock:
            self.stats.submitted += 1
            if self.stats.in_flight >= self.max_workers:
                self.stats.saturated += 1

        def _run_in_thread() -> Any:
            wait_ns = time.perf_counter_ns() - queued_at
            with self._lock:
                self.stats.in_flight += 1
                if self.stats.in_flight > self.stats.peak_in_flight:
                    self.stats.peak_in_flight = self.stats.in_flight
                self.stats.total_queue_wait_ns += wait_ns
                if wait_ns > self.stats.max_queue_wait_ns:
                    self.stats.max_queue_wait_ns = wait_ns
            try:
                return ctx.run(lambda: fn(**kwargs))
            finally:
                with self._lock:
                    self.stats.in_flight -= 1
                    self.stats.completed += 1

        return await loop.run_in_executor(self._pool, _run_in_thread)

    def snapshot(self) -> dict:
        with self._lock:
            return {"name": self.name, **self.stats.as_dict(self.max_workers)}

    def shutdown(self, wait: bool = False) -> None:
        self._pool.shutdown(wait=wait)


# ---------------------------------------------------------------------------
# Process-wide tool executor
# ---------------------------------------------------------------------------

_TOOL_EXECUTOR: Optional[BoundedExecutor] = None
_TOOL_LOCK = threading.Lock()


def get_tool_executor() -> BoundedExecutor:
    """The pool every blocking tool call and compensation runs on. Created on
    first use so importing the library starts no threads."""
    global _TOOL_EXECUTOR
    if _TOOL_EXECUTOR is None:
        with _TOOL_LOCK:
            if _TOOL_EXECUTOR is None:
                _TOOL_EXECUTOR = BoundedExecutor()
    return _TOOL_EXECUTOR


def configure_tool_executor(*, max_workers: int) -> BoundedExecutor:
    """Resize the tool pool. The old pool is shut down without waiting, so
    in-flight compensations finish on their own threads rather than being
    cancelled -- abandoning a half-run compensation would be far worse than
    briefly holding two pools."""
    global _TOOL_EXECUTOR
    with _TOOL_LOCK:
        old = _TOOL_EXECUTOR
        _TOOL_EXECUTOR = BoundedExecutor(max_workers=max_workers)
        if old is not None:
            old.shutdown(wait=False)
        return _TOOL_EXECUTOR


def set_tool_executor(executor: Optional[BoundedExecutor]) -> None:
    """Inject a pool (or None to reset to the lazy default)."""
    global _TOOL_EXECUTOR
    with _TOOL_LOCK:
        _TOOL_EXECUTOR = executor


def tool_executor_stats() -> dict:
    """Point a metrics scrape at this. A non-zero `saturated` with a rising
    `max_queue_wait_ms` means compensations are queueing -- widen the pool."""
    return get_tool_executor().snapshot()


def new_wal_executor(name: str = "agent-saga-wal") -> ThreadPoolExecutor:
    """A private single-thread pool for one WAL's flush loop.

    One thread because the WAL is a single writer and its batches must stay
    ordered. Private because a starved fsync stalls every durable saga in the
    process, and that must be impossible regardless of what tool calls are doing.
    """
    return ThreadPoolExecutor(max_workers=1, thread_name_prefix=name)


__all__ = [
    "BoundedExecutor",
    "ExecutorStats",
    "get_tool_executor",
    "set_tool_executor",
    "configure_tool_executor",
    "tool_executor_stats",
    "new_wal_executor",
]
