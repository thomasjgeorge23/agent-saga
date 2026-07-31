# agent-saga — benchmarks and methodology

Every number here was produced by a script in [`bench/`](../bench) that you can
run yourself, on hardware described below. A benchmark without its methodology
is a marketing number, and this project's whole argument is that you should not
have to take a claim on trust.

**Run them:**

```bash
python bench/bench_core.py --samples 4000 --label mine   # end-to-end overhead
python bench/bench_wal.py                                # WAL, against the device floor
```

---

## What is measured, and what is not

**Measured: the overhead agent-saga adds.** Every profile runs against a no-op
forward function and is reported against a baseline of the same no-op invoked
bare. If your tool call takes 400 ms of network, none of that appears here — the
question is what the library costs *on top of* whatever you were already doing.

**Never blended: the fast path and the durable path.** One is an in-process
deque push, the other is an `fsync`. They are different products in the same
library, and a single average would flatter both dishonestly. They are always
reported separately.

**Not measured:** throughput of your database, your model provider's latency, or
anything at a scale this project has not actually run. There is no
"billions of events" figure here because nobody has run billions of events
through it.

---

## Environment for the figures below

| | |
|---|---|
| Python | 3.12.10 |
| OS | Windows 11, AMD64 |
| Timer | `perf_counter_ns`, 100 ns resolution, monotonic |
| GC | enabled (a `--no-gc` variant is also recorded in `bench/results-gcoff.json`) |
| Samples | 4,000 per profile, after 500 warmup |

**These numbers are reproducible on this machine and are not portable to
yours.** `fsync` latency in particular is a property of your device and
filesystem; a datacentre NVMe and a laptop SSD differ by more than an order of
magnitude. That is why the WAL benchmark reports your device's own floor
alongside agent-saga's cost — the ratio travels even when the absolute does not.

---

## End-to-end step overhead

```
profile                                  n    p50 ms    p95 ms    p99 ms    max ms   overhead
baseline (bare await, no saga)        4000    0.0001    0.0002    0.0002     0.001         --
FAST PATH (REVERSIBLE)                4000    0.0170    0.0216    0.0369     8.174     0.0169
DURABLE PATH (COMPENSABLE)            4000    7.9276   11.9876   39.8754    73.832     7.9275
```

**The fast path costs ~17 µs.** That is a `REVERSIBLE` step: gated, logged to an
in-memory buffer, compensable in-process. For an agent whose tools take
milliseconds at best, it is free in any sense that matters.

**The durable path costs ~7.9 ms, and should.** That is a `COMPENSABLE` step:
the intent is `fsync`ed before the effect fires. This is the price of a record
that survives `SIGKILL`, and a library that made it free would be lying about
durability rather than achieving it.

---

## Group commit: why the durable number is not the whole story

Measured serially, every step waits for its own `fsync`. Under concurrency the
WAL batches many waiters into one:

```
COMPENSABLE @ 1 concurrent sagas      p50  9.86 ms       91 ops/sec
COMPENSABLE @ 8 concurrent sagas      p50 11.19 ms      666 ops/sec
COMPENSABLE @ 64 concurrent sagas     p50 29.96 ms    1,992 ops/sec
COMPENSABLE @ 256 concurrent sagas    p50 94.72 ms    2,856 ops/sec
```

Throughput rises **31x** from 1 to 256 concurrent sagas while per-saga latency
rises 9.6x — the classic group-commit trade. Read it honestly in both
directions: if you run one saga at a time, ~91/sec on this hardware is what you
get, and no amount of concurrency tuning changes a serial workload.

---

## WAL: agent-saga's cost against the device floor

The number that actually travels between machines is the *marginal* cost —
what agent-saga adds over what your disk was going to charge anyway.

```
append, plaintext                0.40 us            1,832,072 ops/sec
append, ENCRYPTED                0.40 us            1,956,373 ops/sec
append+fsync, plaintext          3.561 ms                 270 ops/sec
append+fsync, ENCRYPTED          4.125 ms                 216 ops/sec
raw os.fsync (device floor)      2.590 ms                 339 ops/sec
```

Durable p50 is 3.561 ms, of which the device's own `fsync` is 2.590 ms.
**agent-saga's marginal cost is 0.97 ms — 27%.** On faster storage the absolute
falls and that ratio is what to expect.

Encryption adds **0.00 µs** to the fast path p50, because Fernet runs on the
flusher thread and the caller does not wait for it. It does add ~0.56 ms to the
durable path, where the caller does wait.

---

## Reading a benchmark critically

Three things worth checking in *any* benchmark, including this one:

1. **Is the baseline in the table?** A latency figure with nothing to subtract
   from it measures the machine, not the library. Ours is the first row.
2. **Is the floor shown?** "3.5 ms per durable write" sounds slow until you see
   that 2.6 ms of it is the disk. Reporting only the total would let us take
   credit for hardware, or blame for it.
3. **Are the paths separated?** Blending a 17 µs path with a 7.9 ms path at any
   ratio produces a number that describes neither.

## What these numbers do not establish

- **Scale.** These are single-node figures. The Postgres and Redis WAL backends
  exist and are tested, but no published figure here covers a multi-node fleet,
  because none has been run.
- **Your hardware.** See the caveat above; run `bench/bench_wal.py` and use your
  own floor.
- **Correctness under failure.** That is a different instrument entirely — see
  [`verification.py`](../agent_saga/verification.py) for exhaustive bounded
  model checking of the rollback invariants, and `tests/test_chaos.py` plus the
  `crash_worker` subprocesses for real `kill -9` recovery.
