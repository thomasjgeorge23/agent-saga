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

Pre-alpha, by SagaOps. Implemented and tested (185 tests; the base suite runs
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

Not yet published to PyPI.

Known-pending, tracked openly (see [SECURITY.md](SECURITY.md)): a shipped
distributed lock backend and async-native connectors. KMS/Vault key resolution
is an intended Enterprise-tier feature, deliberately absent from this BYOK core.

## License

Licensed under the GNU Affero General Public License v3.0 only (AGPL-3.0-only).
See [LICENSE](LICENSE). Copyright (c) 2026 SagaOps.

The AGPL's network-use clause (section 13) means a hosted service built on this
code must offer its users the corresponding source. If that does not fit your
deployment, contact SagaOps about a commercial license.
