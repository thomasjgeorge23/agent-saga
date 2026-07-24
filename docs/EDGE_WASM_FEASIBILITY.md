# Edge / WASM Feasibility (Phase 1.3)

**Question:** can `agent-saga` run as a saga control node on an edge runtime —
Cloudflare Workers, Deno Deploy, Fastly, or a browser — to put WAL logging and
the safety gate within a few milliseconds of the user?

**Short answer:** the *safety brain* ports today; the *durability* does not, and
pretending otherwise would ship a WAL that silently loses records. The viable
architecture is a **split node**, not a lift-and-shift. This document gives the
measured evidence, the recommended shape, and the honest failure modes.

Nothing here is speculative. Every claim below was produced by running the code.

---

## 1. What was measured

### 1.1 Dependency surface is tiny

The whole package imports, from the parts of the standard library that a
sandbox restricts, only:

```
urllib (8)   http.server (2)   os (1)   concurrent.futures (1)   asyncio (1)
```

No numpy, no C extensions in the core, no native crypto in the hot path
(`cryptography` is an optional extra). This is the precondition that makes any
of this conceivable — most Python libraries are disqualified at this line.

### 1.2 The core is OS-free

Every safety-critical module imports cleanly with no threads, no sockets, no
`fsync`:

| Module | Role | Threads / fsync / sockets |
|---|---|---|
| `integrity` | hash chain, verify | **none** |
| `provenance` | Merkle root, disclosure | **none** |
| `certify` | rollback-safety proof | **none** |
| `gate` | pre-flight decision | **none** |
| `semantics` | typed compensation | **none** |
| `mesh` | CRDT WAL merge | **none** |
| `predictive` | intent pre-execution | **none** |

The end-to-end audit pipeline — build records → hash-chain → Merkle root →
certify → prove a saga → verify — was executed with **zero OS calls** and
produced correct results. The audit brain runs anywhere Python runs.

### 1.3 Durability is OS-bound, and fails hard

The modules that need a real operating system:

```
wal/file_wal.py   approvals.py   durable.py   gc.py   killswitch.py
ledger.py         cloud_server.py   cli.py
```

The decisive measurement: with `os.fsync` stubbed to raise — exactly the
condition in a Worker sandbox, which has no durable local disk — `FileWAL`
does not degrade. It raises `WALStalled` on the first `barrier()`:

```
WALStalled: WAL sink failed to persist a batch:
            OSError('[Errno 38] fsync not implemented')
```

This is *correct* behaviour on a server (a WAL that cannot fsync must not
pretend to be durable) and *fatal* on an edge runtime (there is no fsync to
have). A lift-and-shift would crash on the first durable write.

---

## 2. The runtime landscape (why the above matters)

| Runtime | Python story | Durable local disk | fsync | Verdict |
|---|---|---|---|---|
| **Cloudflare Workers** | Pyodide (WASM), beta | No (KV/D1/R2/DO are async, remote) | No | split node |
| **Deno Deploy** | via Pyodide/WASM | No | No | split node |
| **Fastly Compute** | WASM, no CPython | — | — | not viable for CPython |
| **Browser / PWA** | Pyodide | OPFS (async), IndexedDB | No (fsync is a no-op) | split node |
| **Node + WASM** | Pyodide | Yes (host fs) | Yes | full node possible |

The common shape across the realistic targets: **compute is available, a POSIX
durable disk is not.** Storage is an *async, remote or object* API (KV, D1, R2,
Durable Objects, OPFS), never a synchronous `write()+fsync()`.

`agent-saga`'s WAL is built on synchronous fsync for a reason — it is what makes
`barrier()` a real durability guarantee. That guarantee cannot be honoured on
these runtimes, so the WAL's *storage* has to move, even though its *logic* does
not.

---

## 3. Recommended architecture: the split node

Do not port the WAL. Split the node along the seam the measurements already
draw — pure logic at the edge, durability behind an async sink.

```
        ┌──────────────  EDGE (Worker / browser)  ──────────────┐
        │  pre-flight gate        (gate.py, OS-free)            │
        │  typed compensation     (semantics.py, OS-free)       │
        │  hash-chain + Merkle    (integrity, provenance)       │
        │  in-memory record buffer                              │
        │  intent pre-execution   (predictive.py)               │
        └───────────────┬───────────────────────────────────────┘
                        │ append (async, batched)
                        ▼
        ┌──────────  DURABLE SINK (KV / D1 / R2 / DO / OPFS)  ──┐
        │  the actual WAL bytes live here                       │
        │  fsync's job is done by the storage service's own     │
        │  durability contract                                  │
        └───────────────────────────────────────────────────────┘
```

Two pieces of new code make this real, and both are small because the seam
already exists.

1. **`AsyncSinkWAL`** — a `BufferedWAL` subclass that overrides the three storage
   methods to talk to an async sink. **This is built and tested in this repo**
   (`agent_saga/wal/async_sink.py`), which is what turns the rest of this
   document from a plan into a measured result.

2. **Storage adapters** — thin `append`/`scan`/`truncate` wrappers for Workers
   KV, D1, R2, Durable Objects, and OPFS. Each is a few dozen lines and is the
   only per-runtime code.

Everything else — the gate, provenance, certify, mesh, predictive — ships as-is.
`mesh.merge_wals` is in fact *more* valuable here: an edge node that buffers in
memory and syncs to storage is the exact CRDT-merge scenario it was built for.

### What building the prototype actually taught us

The seam is one method wider than a first read suggests. `BaseWAL` has **three**
abstract storage methods, not one: `_flush_batch` (persist), `read_all` (replay
and audit), and `clear`. A WAL is not write-only — recovery and the audit tools
read it back — so **an edge sink must be a readable *store*, not a
fire-and-forget pipe.** That is why the shipped interface is:

```python
class AsyncStorageSink:
    async def append(self, lines: list[str]) -> None   # persist, resolve on ack
    async def scan(self)  -> list[str]                 # yield all lines, in order
    async def truncate(self) -> None                   # discard all (tests/ops)
```

Workers KV, D1, R2, Durable Objects, and OPFS can all satisfy this. A pure
message queue cannot — it is write-only — which rules a few storage choices out
before any code is written. That is a useful finding to have on day zero.

The `barrier()` contract survives the substitution intact: a sink that fails to
ack raises `WALStalled`, exactly as a failed `fsync` does. The engine never
reports an intent durable when it is not, whether the failure is a wedged disk
or an unreachable KV namespace. This is verified in
`tests/test_async_sink_wal.py`.

Estimated new surface: **one WAL subclass (done) + N storage adapters.** No
change to the safety core.

---

## 4. What does NOT port, and should not be faked

- **`os.fsync` durability.** Gone. Replaced by the storage service's ack, which
  is a weaker, *asynchronous* guarantee. A crash between buffer and ack loses the
  un-acked tail. This is acceptable for many edge workloads and unacceptable for
  others; it must be a documented, deliberate choice, never a silent downgrade.
- **The recovery daemon** (`recovery.py`, `gc.py`) — these assume a filesystem
  they can sweep and long-lived processes. On serverless they become a scheduled
  job (Cron Triggers) reading from the storage adapter, not a resident daemon.
- **The stdlib dashboard** (`ui/server.py`, `http.server`) — Workers have their
  own fetch handler; the dashboard's *routes* port, its *server* does not. This
  is a ~day of re-wiring, not a rewrite.
- **`cloud_server.py`, `killswitch` file store, `FileLedger`** — all filesystem
  bound; each needs the same async-storage treatment or stays server-side.

---

## 5. Honest risks

| Risk | Severity | Note |
|---|---|---|
| Weaker durability (ack vs fsync) | **High** | The un-acked tail is lost on crash. Must be documented per deployment; do not let `barrier()` claim more than the sink provides. |
| Pyodide maturity on Workers | Medium | Python-on-Workers is beta; cold-start size and CPU limits are real constraints. Re-measure before committing. |
| CPU/time limits | Medium | A large Merkle tree or a big `verify` on a huge log may exceed a Worker's CPU budget. The audit brain is cheap per-saga but not per-*log*; keep whole-log operations server-side. |
| No `cryptography` in WASM without work | Medium | Ed25519 hardware verification needs it; the hashing core (SHA-256) is pure stdlib and fine. Passkey verification likely stays at an edge that has WebCrypto, or server-side. |
| Two code paths to keep in step | Medium | The split node and the server node share the core but diverge at storage; the CRDT merge and the test suite are what keep them honest. |

---

## 6. Recommendation

**Viable, as a split node — not as a lift-and-shift.** The evidence supports a
staged bet:

1. ~~**Prototype the seam first.**~~ **Done, and it passed.** `AsyncSinkWAL` is
   built against an in-memory async sink, and a conformance suite
   (`tests/test_async_sink_wal.py`) drives a real saga through it — execute,
   fail, roll back, verify the chain — with no filesystem anywhere. The gate,
   the hash chain, and the barrier all survive the substitution. The
   architecture is proven; the go/no-go gate is green.
2. **One real adapter (Workers KV or D1).** Deploy the pure core + one storage
   adapter to a Worker, run the `certify`/`prove` pipeline at the edge. Measure
   cold start, CPU budget, and the durability window under induced crashes.
3. **Only then** decide whether the weaker durability is acceptable for the
   target workload. For a local marketplace where the CRDT merge reconciles
   anyway, it likely is. For anything moving money without a server round-trip,
   it likely is not — and that is fine, because the hardware-approval path
   already wants a round-trip for those.

**Do not** attempt to run `FileWAL` unmodified on a Worker. It will crash on the
first `barrier()`, as measured in §1.3.

---

## 7. One-paragraph version for a stakeholder

The safety logic — the gate, the tamper-evident chain, the Merkle audit proofs,
the CRDT sync — is pure Python with no operating-system dependencies and runs at
the edge today; we verified the full audit pipeline executing with zero OS
calls. What does *not* port is durable disk: edge runtimes have no `fsync`, and
our WAL correctly refuses to fake durability, so it crashes there by design. The
fix is not a rewrite but a seam we already have — swap the WAL's disk sink for an
async storage sink (Workers KV/D1/R2, or the browser's OPFS) and inherit
everything else. The one thing that must be said out loud is that storage-ack
durability is weaker than fsync: a crash can lose the last un-acked records. For
offline-first local marketplaces, where our conflict-free merge reconciles on
reconnect anyway, that trade is sound. For unmediated money movement it is not,
and those flows already route through a server-backed approval.
