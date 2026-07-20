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

## Status

Pre-alpha. Core engine, recovery daemon, and three reference connectors are
implemented and tested (72 tests, no external dependencies to run the suite).
Not yet published to PyPI. Snapshot-based `REVERSIBLE` capture and framework
adapters (LangGraph, CrewAI, OpenAI Agents SDK) are next.

Apache 2.0.
