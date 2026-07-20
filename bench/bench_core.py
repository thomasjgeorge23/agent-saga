"""Latency benchmark for the agent-saga hot path.

Measures the *overhead the library adds*, not the wall time of the tool call.
Every profile therefore runs against a no-op forward function and is reported
against a baseline of the same no-op invoked bare.

The fast path and the durable path are reported separately and never blended.
They are different products in the same library: one is an in-process deque
push, the other is an fsync. A single average would flatter both dishonestly.

Run:  python bench/bench_core.py [--samples 10000]
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_saga import (  # noqa: E402
    ActionSemantics,
    AsyncWAL,
    Compensation,
    SagaContext,
    compensator,
)

NS_PER_MS = 1_000_000


# --------------------------------------------------------------------------
# The workload. Deliberately trivial: we are measuring our own overhead.
# The compensation is registry-backed so the benchmark exercises the same
# recoverable path a production deployment would.
# --------------------------------------------------------------------------

async def noop():
    return {"id": "rec_1"}


@compensator("bench.noop")
async def _undo(record_id):
    return None


def _compensation(result):
    return Compensation(fn=_undo, handler="bench.noop",
                        kwargs={"record_id": result["id"]}, description="noop")


class Stats:
    def __init__(self, name: str, samples_ns: list[int], baseline_ns: float | None = None):
        self.name = name
        s = sorted(samples_ns)
        self.n = len(s)
        self.p50 = s[int(self.n * 0.50)] / NS_PER_MS
        self.p95 = s[int(self.n * 0.95)] / NS_PER_MS
        self.p99 = s[int(self.n * 0.99)] / NS_PER_MS
        self.max = s[-1] / NS_PER_MS
        self.mean = statistics.fmean(s) / NS_PER_MS
        self.baseline = baseline_ns / NS_PER_MS if baseline_ns else None

    @property
    def overhead_p50(self) -> float | None:
        return None if self.baseline is None else self.p50 - self.baseline

    def row(self) -> str:
        oh = "--" if self.overhead_p50 is None else f"{self.overhead_p50:8.4f}"
        return (f"{self.name:<34} {self.n:>7} {self.p50:>9.4f} {self.p95:>9.4f} "
                f"{self.p99:>9.4f} {self.max:>9.3f} {oh:>10}")

    def as_dict(self) -> dict:
        return {"profile": self.name, "samples": self.n, "p50_ms": round(self.p50, 5),
                "p95_ms": round(self.p95, 5), "p99_ms": round(self.p99, 5),
                "max_ms": round(self.max, 4), "mean_ms": round(self.mean, 5),
                "overhead_p50_ms": None if self.overhead_p50 is None else round(self.overhead_p50, 5)}


# --------------------------------------------------------------------------
# Profiles
# --------------------------------------------------------------------------

async def bench_baseline(samples: int) -> tuple[Stats, float]:
    """Bare `await noop()`. Everything else is measured against this."""
    for _ in range(1000):
        await noop()
    lat = []
    for _ in range(samples):
        t0 = time.perf_counter_ns()
        await noop()
        lat.append(time.perf_counter_ns() - t0)
    st = Stats("baseline (bare await, no saga)", lat)
    return st, st.p50 * NS_PER_MS


async def bench_path(name: str, semantics: ActionSemantics, samples: int,
                     baseline_ns: float, tmp: Path) -> tuple[Stats, dict]:
    wal = AsyncWAL(tmp / f"{semantics.value.lower()}.jsonl")
    await wal.start()
    ctx = SagaContext(wal=wal)

    for _ in range(500):  # warmup: JIT-free but still primes caches and the flusher
        await ctx.execute(tool="noop", semantics=semantics, forward=noop,
                          compensate=_compensation)

    b0, f0 = wal.barriers, wal.dropped
    lat = []
    for _ in range(samples):
        t0 = time.perf_counter_ns()
        await ctx.execute(tool="noop", semantics=semantics, forward=noop,
                          compensate=_compensation)
        lat.append(time.perf_counter_ns() - t0)

    telemetry = {"wal_events": wal._seq, "fsync_barriers": wal.barriers - b0,
                 "backpressure_drops": wal.dropped - f0}
    await wal.close()
    return Stats(name, lat, baseline_ns), telemetry


async def bench_concurrency(concurrency: int, per_task: int, tmp: Path) -> dict:
    """Shared WAL, N concurrent sagas. Measures queue contention and whether
    the single-writer flusher becomes the bottleneck."""
    wal = AsyncWAL(tmp / "concurrent.jsonl")
    await wal.start()
    lat: list[int] = []

    async def worker():
        ctx = SagaContext(wal=wal)
        for _ in range(per_task):
            t0 = time.perf_counter_ns()
            await ctx.execute(tool="noop", semantics=ActionSemantics.COMPENSABLE,
                              forward=noop, compensate=_compensation)
            lat.append(time.perf_counter_ns() - t0)

    t0 = time.perf_counter()
    await asyncio.gather(*[worker() for _ in range(concurrency)])
    elapsed = time.perf_counter() - t0

    total = concurrency * per_task
    st = Stats(f"COMPENSABLE @ {concurrency} concurrent sagas", lat)
    result = {"concurrency": concurrency, "total_ops": total,
              "elapsed_s": round(elapsed, 3), "ops_per_sec": round(total / elapsed, 1),
              "waiters": wal.barriers, "fsyncs": wal.flush_cycles,
              "backpressure_drops": wal.dropped,
              # waiters per fsync: 1.0 means no batching, N means N sagas
              # shared one disk round trip.
              "group_commit_factor": round(wal.barriers / max(wal.flush_cycles, 1), 2),
              **st.as_dict()}
    await wal.close()
    return result, st


# --------------------------------------------------------------------------

async def main(samples: int, no_gc: bool, label: str) -> None:
    # Timer granularity differs by platform (Windows perf_counter is ~100ns,
    # Linux ~1ns). Print it so a reader can tell a real tail from a tick artifact.
    clock = time.get_clock_info("perf_counter")

    if no_gc:
        # Isolating the fast-path tail: if the ~6ms outlier vanishes here, it
        # was a CPython collection pause, not our code.
        gc.disable()
        gc.collect()

    gc_before = gc.get_stats()

    print(f"\nagent-saga latency benchmark  [{label}]")
    print(f"  python   {platform.python_version()}  |  {platform.system()} {platform.release()}"
          f"  |  {platform.machine()}")
    print(f"  timer    perf_counter_ns, resolution {clock.resolution * 1e9:.1f} ns, "
          f"monotonic={clock.monotonic}")
    print(f"  gc       {'DISABLED' if no_gc else 'enabled'}")
    print(f"  samples  {samples:,} per profile (+500 warmup)\n")

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        base, base_ns = await bench_baseline(samples)
        fast, fast_tel = await bench_path("FAST PATH (REVERSIBLE)",
                                          ActionSemantics.REVERSIBLE, samples, base_ns, tmp)
        dur, dur_tel = await bench_path("DURABLE PATH (COMPENSABLE)",
                                        ActionSemantics.COMPENSABLE, samples, base_ns, tmp)

        hdr = (f"{'profile':<34} {'n':>7} {'p50 ms':>9} {'p95 ms':>9} "
               f"{'p99 ms':>9} {'max ms':>9} {'overhead':>10}")
        print(hdr)
        print("-" * len(hdr))
        for st in (base, fast, dur):
            print(st.row())

        print(f"\n  fast path    -> wal events {fast_tel['wal_events']:,}, "
              f"fsync barriers {fast_tel['fsync_barriers']}, "
              f"drops {fast_tel['backpressure_drops']}")
        print(f"  durable path -> wal events {dur_tel['wal_events']:,}, "
              f"fsync barriers {dur_tel['fsync_barriers']:,}, "
              f"drops {dur_tel['backpressure_drops']}")

        print("\nconcurrency")
        print("-" * len(hdr))
        conc_results = []
        for c in (1, 8, 64, 256):
            res, st = await bench_concurrency(c, max(samples // c // 4, 20), tmp)
            conc_results.append(res)
            print(f"{st.name:<34} {st.n:>7} {st.p50:>9.4f} {st.p95:>9.4f} "
                  f"{st.p99:>9.4f} {st.max:>9.3f} {res['ops_per_sec']:>9,.0f}/s")
        print(f"{'':<34} {'':>7} {'':>9} {'':>9} {'':>9} {'':>9} {'ops/sec':>10}")

        gc_collections = sum(g["collections"] for g in gc.get_stats()) - \
            sum(g["collections"] for g in gc_before)

        out = {
            "environment": {"python": platform.python_version(), "os": platform.system(),
                            "release": platform.release(), "machine": platform.machine(),
                            "timer_resolution_ns": clock.resolution * 1e9,
                            "gc_enabled": not no_gc,
                            "gc_collections_during_run": gc_collections},
            "label": label,
            "samples": samples,
            "profiles": [base.as_dict(), fast.as_dict(), dur.as_dict()],
            "telemetry": {"fast_path": fast_tel, "durable_path": dur_tel},
            "concurrency": conc_results,
            "caveat": ("fsync latency is hardware- and filesystem-specific. The durable "
                       "path number is not portable and must be re-measured per deployment "
                       "target before being quoted."),
        }
        print(f"\n  gc collections during run: {gc_collections}")
        dest = Path(__file__).parent / f"results-{label}.json"
        dest.write_text(json.dumps(out, indent=2), encoding="utf-8")
        print(f"  written -> {dest}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=10_000)
    ap.add_argument("--no-gc", action="store_true",
                    help="disable CPython GC to isolate collection pauses from the tail")
    ap.add_argument("--label", default="local", help="output file suffix")
    args = ap.parse_args()
    asyncio.run(main(args.samples, args.no_gc, args.label))
