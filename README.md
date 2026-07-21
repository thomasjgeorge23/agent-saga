# agent-saga

**The undo button for AI agents.** Transactional boundaries, typed compensation
semantics, and a pre-flight safety gate for autonomous agents that call real
APIs, mutate real databases, and move real money.

When an agent hallucinates halfway through a multi-step task, the side effects
it already caused are still real. `agent-saga` wraps each tool call, records a
runtime-derived inverse action, and unwinds the whole transaction — in-process
on failure, or from a separate recovery daemon if the process itself dies.

```bash
python examples/demo.py   # no credentials, no database, no network required
```

---

## Why this is not just the Saga pattern, or just Temporal

**Undo is not one thing.** Every side effect is classified:

| Semantics | Meaning | Example |
|---|---|---|
| `REVERSIBLE` | Restored exactly; no observer can tell | in-memory cache write |
| `COMPENSABLE` | Offset by an inverse, but the trace is permanent | Stripe refund, CRM field revert |
| `IRREVERSIBLE` | No automated undo exists | outbound email, wire transfer |

**The compensation is derived at runtime, not declared up front.** A workflow
engine makes you hard-code the compensating step when you write the code. But an
LLM agent chooses the tool at runtime, and the inverse depends on the result —
you can't refund a `charge_id` you haven't seen yet. Compensations here are
factories: `(forward_result) -> Compensation`.

**The pre-flight gate is the product.** A bank does not buy a post-disaster
cleanup script; it buys a control that refuses to enter an uncompensable
boundary without a human on the hook. The gate runs *before* any side effect —
the only point at which refusal is free.

```python
from agent_saga import saga, tool, ActionSemantics, Compensation

@tool(semantics=ActionSemantics.COMPENSABLE,
      compensate=lambda result: Compensation(
          fn=refund, handler="stripe.refund",
          kwargs={"charge_id": result["id"]}))
def charge_customer(customer_id, amount):
    return stripe.Charge.create(customer=customer_id, amount=amount)

@saga
async def agent_run():
    await charge_customer(customer_id="cus_1", amount=4200)
    await update_crm(record_id="acct_1", status="customer")
    # any exception here rolls back the charge (refund) AND the CRM edit, LIFO
```

---

## Crash recovery

A write-ahead log nobody reads is just an audit file. If a process is `SIGKILL`ed
after a charge's intent is durable, that charge is orphaned until an independent
process resolves it. `saga-recoveryd` scans the WAL, and:

- claims work only when a saga's **lease has expired** (a live process is never
  touched; a PID would lie, leases don't);
- resolves each dangling step through a **named registry handler** — a closure
  cannot cross a process boundary, so compensations declare a stable name with
  JSON-serializable kwargs;
- uses **deterministic recovery tokens** so two daemons can never double-compensate;
- **fails closed**: an `IRREVERSIBLE` step, an unrecoverable compensation, or a
  handler the daemon hasn't imported all escalate to a human queue rather than
  guess.

> The daemon must import the same connector packages as the agent. If it doesn't,
> every dangling saga escalates with `handler not registered` — by design.

---

## Connectors

Reference implementations, each honest about what it cannot undo:

- **Stripe** (`COMPENSABLE`) — charge with a deterministic refund key; treats
  `charge_already_refunded` as success so a late-returning daemon doesn't loop.
- **PostgreSQL** (`COMPENSABLE`) — snapshots the affected columns in one
  autocommit round trip (never holds a transaction across the model's thinking
  time), and restores only if no concurrent writer touched the row.
- **Salesforce** (`COMPENSABLE`) — reverts only the patched fields, filtered to
  writable ones, guarded by `LastModifiedDate`.

**Credentials never enter the WAL.** Compensation kwargs are fsynced in plaintext
and read by another process, so connectors pass a credential *reference*
(`credential_ref="stripe_prod"`) resolved from the daemon's own secret store at
use time. `assert_no_secrets()` raises at authoring time if a secret slips in.

```python
from agent_saga.connectors import set_credential_resolver
set_credential_resolver(lambda ref: vault.read(f"agents/{ref}"))
```

---

## Performance

In-process overhead, measured (`bench/bench_core.py`). Two profiles, never blended:

| Path | p50 | p95 | Notes |
|---|---|---|---|
| `REVERSIBLE` (fast) | ~10 µs | ~15 µs | lock-free append, no fsync |
| `COMPENSABLE` (durable) | ~3–6 ms | — | two fsync barriers; **hardware-specific** |

Under concurrency the durable path **group-commits**: N concurrent sagas in one
flush window share a single fsync, so throughput scales ~linearly (measured 300 →
33k ops/s from 1 → 256 concurrent sagas) while p99 stays bounded.

> Durable-path latency is a property of your disk, not this library. The numbers
> above are from Windows/NTFS on a dev machine and **must be re-measured on your
> deployment target** before you quote them. CI re-runs the benchmark on Linux
> and reports median-of-p99 across runs.

---

## LangGraph

Drop it into an existing graph without rewriting anything. `wrap_tool` returns a
`StructuredTool` with the same name, description, and args schema, so the model
and `ToolNode` see no difference; `saga_run` makes the whole graph run one
transaction.

```python
from agent_saga.adapters.langgraph import wrap_tool, saga_run
from agent_saga import ActionSemantics, Compensation

safe_charge = wrap_tool(
    charge_tool,                       # your existing @tool
    semantics=ActionSemantics.COMPENSABLE,
    compensate=lambda r: Compensation(
        fn=refund, handler="stripe.refund", kwargs={"charge_id": r["id"]}))

# build your graph with safe_charge in place of charge_tool, then:
result = await saga_run(graph, {"messages": [...]})
# if any node raises, every tool that already ran is compensated LIFO
```

## Time-travel debugger

A zero-dependency visual debugger reads any WAL and reconstructs each run:

```bash
agent-saga ui --wal-path ./agent-saga.wal --port 8080
# or: python -m agent_saga.ui --wal-path ./agent-saga.wal
```

Dark enterprise UI (no build step, no `node_modules`, stdlib HTTP server): a
sidebar of runs filterable by status, a LIFO timeline colour-coded by outcome
(committed / compensated / orphaned / failed), and an inspector showing each
step's semantics, forward kwargs, and the exact compensation that ran — with
credentials shown as references, never values. Binds to `127.0.0.1` by default.

## Status

Pre-alpha, by SagaOps. Implemented and tested (277 tests; the base suite runs
with only `pytest`; optional extras add their own SDKs):

- Core engine, recovery daemon (truncation-tolerant), and a time-travel debugger
  with optional bearer-token auth for shared environments.
- Connectors: Stripe, Postgres (full CRUD — update/insert/delete with compound
  primary keys), Salesforce.
- Adapters: LangGraph, CrewAI, OpenAI Agents SDK, LlamaIndex, AutoGen.
- Snapshot capture: in-process (`REVERSIBLE`) and durable crash-recoverable
  (`COMPENSABLE`), with a conservative store GC sweep.
- Durability & safety: configurable WAL backpressure (`RAISE` by default —
  never silently drops a record), and optional BYOK WAL-at-rest encryption
  (`pip install agent-saga[encryption]`; key via `AGENT_SAGA_WAL_KEY` or an
  injected encryptor — a reader without the key fails loud, never silent).
- Recovery locking: an injectable lock interface, defaulting to a local file
  lock (no Redis in-tree — supply a distributed backend if you run a fleet).
- Pluggable WAL backends behind `BaseWAL`: `FileWAL` (the zero-dependency
  default, fsync-durable) and `RedisWAL` for multi-node deployments
  (`pip install agent-saga[redis]`). `barrier()` is part of the interface, not
  an extra — a backend without a durability fence is fire-and-forget. Redis is
  documented as a *weaker* durability class than fsync and supports `WAIT` for
  replica acknowledgment; read that section before putting money through it.
- Deterministic idempotency: compensation keys are `SHA-256(saga_id, step_id,
  scope)` — stable across processes, hosts and restarts, and deliberately *not*
  keyed on attempt count (a key that varied per retry would make attempt 2 look
  like a fresh refund). The key is auto-injected into handlers that accept it,
  and an execution ledger reads both the daemon journal and the crashed
  process's own WAL, so completed work is skipped rather than repeated.
- `RedisWAL` stamps a global sequence (`gseq`) from a shared Redis counter, one
  INCRBY per batch, so records on a multi-node log are uniquely identified and
  globally ordered. The per-process `seq` is left intact for fence bookkeeping.
- OpenTelemetry spans (`pip install agent-saga[opentelemetry]`): a root
  `saga.execute` span with `saga.status` (COMPLETED / ROLLED_BACK / FAILED),
  child `saga.step.<tool>` and `saga.rollback.<tool>` spans, exceptions
  recorded with ERROR status, and trace/span ids stamped onto every log record
  so logs and traces join in either direction. Opt-in via `setup_telemetry()`;
  a `NoOpTracer` is the default and importing the package never touches
  `opentelemetry`.
- Tentative resources are crash-durable: registration is written to the WAL
  with a named rollback handler, so a resource stranded PENDING by a SIGKILL is
  found and settled by the recovery daemon. An in-process-only rollback is
  escalated rather than guessed at.
- Saga isolation countermeasures: `TentativeResource` marks a business entity
  PENDING for the saga's life and resolves it to COMMITTED / ROLLED_BACK
  automatically at the boundary; `SemanticLockManager` lets a saga claim a
  resource id so a concurrent saga cannot dirty-read it. Locks release on every
  exit path, including abort. Both process-local by default — inject a shared
  implementation for multi-node -- `RedisSemanticLocks` ships in-tree
  (`agent-saga[redis]`): `SET NX PX` for atomic acquire with a self-expiring
  lease so a SIGKILLed holder cannot deadlock a resource, compare-and-delete
  release via Lua so one saga can never free another's claim, and background
  lease renewal so a long agent run does not lose its lock mid-transaction.
- The recovery ledger is pluggable (`FileLedger` default, `RedisLedger` for a
  fleet). A node-local ledger behind a shared WAL means two daemons cannot see
  each other's successes and may compensate twice; the daemon warns when it
  detects that combination.
- Recovery is backend-agnostic: `RecoveryDaemon` accepts a path or any
  `BaseWAL`, so a daemon on one node can resolve a saga orphaned on another.
- No unbounded waits: `barrier()` raises `WALStalled` rather than hanging
  forever on a wedged device, and a sink error fails pending fences immediately
  with the real cause. `close()` is bounded too.
- Thread isolation: each WAL owns a private flusher thread, so a burst of slow
  connector calls can never starve fsync and stall unrelated sagas. Blocking
  tool work runs on a bounded, resizable pool that reports its own saturation
  (`tool_executor_stats()`), instead of silently queueing on asyncio's default
  executor. Async-native compensations (Salesforce, Postgres via `asyncpg`)
  skip the thread hop entirely; `AGENT_SAGA_PG_DRIVER` pins the Postgres driver
  so a transitive `asyncpg` install cannot silently change it.
- Observability: `saga_id` / `step_id` correlation ids stamped on every log
  record via contextvars (concurrent sagas never bleed ids), with a text
  formatter for incidents and a JSON one for log pipelines. `configure_logging()`
  is opt-in and never touches the root logger.

Not yet published to PyPI.

Known-pending, tracked openly (see [SECURITY.md](SECURITY.md)): a shipped
distributed lock backend and async-native connectors. KMS/Vault key resolution
is an intended Enterprise-tier feature, deliberately absent from this BYOK core.

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
Copyright 2026 SagaOps.

Permissive on purpose. This library is `import`ed directly into the process that
moves your money, and a copyleft dependency on that path is something most legal
teams will not clear — so the thing designed to be trusted with production
traffic is licensed so that it can actually reach production. Apache-2.0 also
carries an **explicit patent grant** (section 3), which is the clause enterprise
review actually looks for.

- **Use it in a closed-source product.** No obligation to publish anything.
- **Run it as a service.** No network-use clause, no source-offer requirement.
- **Fork it.** Genuinely — just rename it, per [TRADEMARKS.md](TRADEMARKS.md).

The code is open; the **name** is not (Apache-2.0 section 6 grants no trademark
rights). SagaOps' commercial products are separate hosted components — they are
not a license upgrade, and nothing in this repository is gated behind one.
