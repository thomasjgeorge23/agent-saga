"""bench_wal.py -- what the write-ahead log actually costs.

This benchmark exists to be *believed*, which means it must be able to return an
unflattering number. It reports two fundamentally different costs and refuses to
average them into one marketing figure:

  FAST PATH (append)      in-process, lock-free deque push. Microseconds. This is
                          what every REVERSIBLE step pays, and what people mean
                          when they ask "does this slow my agent down".

  DURABLE PATH (barrier)  append + flush + fsync. Milliseconds, and bounded by
                          your disk, not by this library. Every COMPENSABLE and
                          IRREVERSIBLE step pays it, on purpose: an intent that
                          is not on disk cannot be recovered after a crash.

Anyone claiming a single "zero overhead" number for a durable log is measuring
the fast path and quietly not calling fsync. So the script also measures the raw
fsync cost of *your* device with no agent-saga in the picture, which lets you
subtract the hardware and see the library's true marginal cost.

    python bench/bench_wal.py                      # 10,000 records per profile
    python bench/bench_wal.py --samples 50000
    python bench/bench_wal.py --json results.json  # machine-readable

Encrypted profiles are skipped with a clear notice when `cryptography` is not
installed (`pip install agent-saga[encryption]`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_saga import AsyncWAL  # noqa: E402

NS_PER_MS = 1_000_000
RAW_FSYNC = "raw os.fsync (device floor)"
RECORD = {
    # A realistic STEP_INTENT payload, not an empty dict: JSON encoding cost
    # scales with the record, and pretending records are tiny understates it.
    "saga_id": "9f2c1e7a4b6d4c8e9a0b1c2d3e4f5a6b",
    "step_id": "3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b",
    "tool": "stripe.charge",
    "semantics": "COMPENSABLE",
    "kwargs": {"customer_id": "cus_acme_42", "amount": 49900, "currency": "usd"},
}


# ===========================================================================
# Statistics
# ===========================================================================

@dataclass
class Stats:
    """Percentiles from raw nanosecond samples.

    Percentiles are read from the sorted sample with a nearest-rank index rather
    than interpolated -- for latency work you want a value that actually
    occurred, not a synthetic point between two observations.
    """

    name: str
    samples_ns: list[int] = field(repr=False, default_factory=list)
    note: str = ""

    def _pct(self, p: float) -> float:
        s = sorted(self.samples_ns)
        if not s:
            return float("nan")
        idx = min(len(s) - 1, max(0, int(round(p * len(s))) - 1))
        return s[idx] / NS_PER_MS

    @property
    def n(self) -> int: return len(self.samples_ns)
    @property
    def p50(self) -> float: return self._pct(0.50)
    @property
    def p95(self) -> float: return self._pct(0.95)
    @property
    def p99(self) -> float: return self._pct(0.99)
    @property
    def maximum(self) -> float: return max(self.samples_ns) / NS_PER_MS if self.samples_ns else float("nan")
    @property
    def mean(self) -> float: return statistics.fmean(self.samples_ns) / NS_PER_MS if self.samples_ns else float("nan")

    @property
    def ops_per_sec(self) -> float:
        total_s = sum(self.samples_ns) / 1e9
        return self.n / total_s if total_s > 0 else float("inf")

    def as_dict(self) -> dict:
        return {"profile": self.name, "samples": self.n,
                "p50_ms": round(self.p50, 6), "p95_ms": round(self.p95, 6),
                "p99_ms": round(self.p99, 6), "max_ms": round(self.maximum, 4),
                "mean_ms": round(self.mean, 6),
                "ops_per_sec": round(self.ops_per_sec, 1), "note": self.note}


def _fmt(value: float) -> str:
    """Latencies here span five orders of magnitude (microseconds to tens of
    milliseconds). A fixed precision would print either 0.000 or noise, so the
    precision follows the magnitude."""
    # ASCII only: a 'µ' becomes mojibake on a Windows cp1252 console, and this
    # script is meant to be run and pasted by strangers.
    if value != value:  # NaN
        return "     n/a"
    if value < 0.001:
        return f"{value * 1000:6.2f}us"  # microseconds
    if value < 1:
        return f"{value:8.4f}"
    return f"{value:8.3f}"


# ===========================================================================
# Profiles
# ===========================================================================

async def _measure_append(path: Optional[Path], samples: int, encryptor,
                          name: str, note: str) -> Stats:
    """FAST PATH: the synchronous in-process append only.

    The flusher still drains in the background, so this measures what the caller
    actually waits for -- which is the honest question. It does not include the
    fsync, because on this path the caller never waits for one.
    """
    wal = AsyncWAL(path, encryptor=encryptor, max_buffer=samples * 2 + 10_000)
    await wal.start()
    try:
        for _ in range(min(500, samples)):        # warm caches and the flusher
            wal.append("STEP_INTENT", RECORD)
        lat: list[int] = []
        for _ in range(samples):
            t0 = time.perf_counter_ns()
            wal.append("STEP_INTENT", RECORD)
            lat.append(time.perf_counter_ns() - t0)
        await wal.barrier()                        # settle before teardown
    finally:
        await wal.close()
    return Stats(name, lat, note)


async def _measure_durable(path: Path, samples: int, encryptor,
                           name: str, note: str) -> Stats:
    """DURABLE PATH: append + barrier, i.e. what a COMPENSABLE step pays.

    Serial by construction: each iteration awaits its own fsync. That is the
    worst case and the one worth publishing -- under real concurrency the WAL
    batches many waiters into a single fsync (group commit) and per-op cost
    falls sharply. Measuring the concurrent case here would flatter us.
    """
    wal = AsyncWAL(path, encryptor=encryptor)
    await wal.start()
    try:
        for _ in range(min(100, samples)):
            seq = wal.append("STEP_INTENT", RECORD)
            await wal.barrier(seq)
        lat: list[int] = []
        for _ in range(samples):
            t0 = time.perf_counter_ns()
            seq = wal.append("STEP_INTENT", RECORD)
            await wal.barrier(seq)
            lat.append(time.perf_counter_ns() - t0)
    finally:
        await wal.close()
    return Stats(name, lat, note)


def _measure_raw_fsync(path: Path, samples: int) -> Stats:
    """The device floor, with no agent-saga involved.

    Without this, a reader cannot tell our overhead from their disk. Subtract
    this from the durable path and what remains is the library's real cost.
    """
    lat: list[int] = []
    line = (json.dumps(RECORD) + "\n").encode()
    with open(path, "wb", buffering=0) as fh:
        for _ in range(min(100, samples)):
            fh.write(line)
            os.fsync(fh.fileno())
        for _ in range(samples):
            fh.write(line)
            t0 = time.perf_counter_ns()
            os.fsync(fh.fileno())
            lat.append(time.perf_counter_ns() - t0)
    return Stats(RAW_FSYNC, lat, "hardware floor")


# ===========================================================================

def _encryptor_or_none():
    try:
        from agent_saga import FernetEncryptor, generate_key
    except Exception:
        return None, "cryptography not installed"
    try:
        return FernetEncryptor(generate_key()), ""
    except ImportError as exc:
        return None, str(exc)


async def run(samples: int, json_path: Optional[str]) -> int:
    clock = time.get_clock_info("perf_counter")
    encryptor, enc_reason = _encryptor_or_none()

    print()
    print("  agent-saga :: write-ahead log latency")
    print(f"  python {platform.python_version()}  |  {platform.system()} "
          f"{platform.release()}  |  {platform.machine()}")
    print(f"  perf_counter resolution {clock.resolution * 1e9:.1f} ns  |  "
          f"{samples:,} samples per profile")
    if encryptor is None:
        print(f"  encrypted profiles SKIPPED: {enc_reason}")
        print("  -> pip install agent-saga[encryption]")
    print()

    results: list[Stats] = []
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)

        # --- fast path -----------------------------------------------------
        results.append(await _measure_append(
            tmp / "fast_plain.wal", samples, None,
            "append, plaintext", "no fsync on this path"))
        if encryptor is not None:
            results.append(await _measure_append(
                tmp / "fast_enc.wal", samples, encryptor,
                "append, ENCRYPTED", "encryption is off the caller's path"))

        # --- durable path --------------------------------------------------
        results.append(await _measure_durable(
            tmp / "dur_plain.wal", samples, None,
            "append+fsync, plaintext", "serial worst case"))
        if encryptor is not None:
            results.append(await _measure_durable(
                tmp / "dur_enc.wal", samples, encryptor,
                "append+fsync, ENCRYPTED", "serial worst case"))

        # --- hardware floor ------------------------------------------------
        results.append(_measure_raw_fsync(tmp / "raw.bin", samples))

    header = (f"  {'profile':<30}{'p50':>10}{'p95':>10}{'p99':>10}"
              f"{'max':>10}{'ops/sec':>12}")
    print(header)
    print("  " + "-" * (len(header) - 2))
    for st in results:
        print(f"  {st.name:<30}{_fmt(st.p50)}{_fmt(st.p95)}{_fmt(st.p99)}"
              f"{_fmt(st.maximum)}{st.ops_per_sec:>12,.0f}")
    print(f"  {'':<30}{'ms':>10}{'ms':>10}{'ms':>10}{'ms':>10}")

    by_name = {s.name: s for s in results}
    fast = by_name.get("append, plaintext")
    durable = by_name.get("append+fsync, plaintext")
    raw = by_name[RAW_FSYNC]

    print()
    print("  How to read this")
    print("  " + "-" * 70)
    if fast:
        print(f"  * Fast path p50 is {_fmt(fast.p50).strip()} -- this is what a REVERSIBLE step")
        print("    costs your agent. For practical purposes it is free.")
    if durable and raw:
        overhead = durable.p50 - raw.p50
        pct = (overhead / durable.p50 * 100) if durable.p50 else 0
        print(f"  * Durable path p50 is {_fmt(durable.p50).strip()} ms, of which your disk's own")
        print(f"    fsync is {_fmt(raw.p50).strip()} ms. agent-saga's marginal cost is "
              f"{_fmt(overhead).strip()} ms ({pct:.0f}%).")
        print("    This path is NOT free, and should not be. It is the price of an")
        print("    intent that survives SIGKILL.")
    enc_fast = by_name.get("append, ENCRYPTED")
    if fast and enc_fast:
        delta = enc_fast.p50 - fast.p50
        print(f"  * Encryption adds {_fmt(delta).strip()} to the fast path p50. Fernet runs on")
        print("    the flusher thread, so the caller mostly does not wait for it.")
    print("  * The durable path here is measured SERIALLY, one fsync per record.")
    print("    Under concurrency the WAL group-commits many waiters into a single")
    print("    fsync and per-record cost drops well below this figure.")
    print()
    print("  Caveat: fsync latency is a property of your device and filesystem.")
    print("  These numbers are reproducible on this machine, not portable to yours.")
    print()

    if json_path:
        payload = {
            "environment": {
                "python": platform.python_version(), "os": platform.system(),
                "release": platform.release(), "machine": platform.machine(),
                "timer_resolution_ns": clock.resolution * 1e9,
                "encrypted_profiles": encryptor is not None,
            },
            "samples": samples,
            "profiles": [s.as_dict() for s in results],
            "caveat": ("fsync latency is hardware- and filesystem-specific; the "
                       "durable path is measured serially (worst case) and "
                       "improves under concurrency via group commit."),
        }
        Path(json_path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"  written -> {json_path}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--samples", type=int, default=10_000,
                    help="records per profile (default: 10000)")
    ap.add_argument("--json", dest="json_path", default=None,
                    help="also write machine-readable results here")
    args = ap.parse_args(argv)
    if args.samples < 100:
        ap.error("--samples must be at least 100 to produce meaningful percentiles")
    return asyncio.run(run(args.samples, args.json_path))


if __name__ == "__main__":
    raise SystemExit(main())
