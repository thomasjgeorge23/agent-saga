# agent-saga

**The undo button for AI agents.** Transactional boundaries, typed compensation
semantics, and a pre-flight safety gate for autonomous agents that provision
infrastructure, write to vector stores, open tickets and PRs, message people —
and, yes, move money.

When an agent hallucinates halfway through a multi-step task, the side effects
it already caused are still real. `agent-saga` wraps each tool call, records a
runtime-derived inverse action, and unwinds the whole transaction — in-process
on failure, or from a separate recovery daemon if the process itself dies.

```bash
python examples/multi_domain.py   # 5 systems, 1 transaction, no network needed
python examples/chaos_demo.py     # optimistic vs. transactional, side by side
```

---

## Why this is not just the Saga pattern, or just Temporal

**Undo is not one thing.** Every side effect is classified:

| Semantics | Meaning | Examples across domains |
|---|---|---|
| `REVERSIBLE` | Restored exactly; no observer can tell | scratch file the saga created, in-process cache, a Terraform plan not yet applied |
| `COMPENSABLE` | Offset by an inverse, but the trace is permanent | terminate an EC2 instance, delete a Pinecone namespace, close a Jira ticket, close a GitHub PR, delete a Slack message, Stripe refund |
| `IRREVERSIBLE` | No automated undo exists | Twilio SMS, SendGrid email, `DROP TABLE` with no snapshot, a wire transfer, a Cloudflare purge that already served stale content |

The classification is the point. Deleting a Slack message is *compensable*; a
push notification already on someone's phone is not. Terminating an instance is
compensable; the hour you were billed for is not. The engine makes you say which
one you have, and refuses to start the third kind without a human.

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
from agent_saga import saga_scope, ActionSemantics, Compensation

C = ActionSemantics.COMPENSABLE

async with saga_scope() as saga:
    # 1. infrastructure
    box = await saga.execute(
        tool="aws.run_instances", semantics=C,
        forward=lambda: ec2.run_instances(ImageId=ami, InstanceType="m6i.large"),
        compensate=lambda r: Compensation(
            fn=terminate, handler="aws.terminate_instance",
            kwargs={"instance_id": r["Instances"][0]["InstanceId"]}))

    # 2. vector store
    await saga.execute(
        tool="pinecone.create_namespace", semantics=C,
        forward=lambda: index.create_namespace("acme"),
        compensate=lambda r: Compensation(
            fn=drop_ns, handler="pinecone.delete_namespace",
            kwargs={"namespace": r["namespace"]}))

    # 3. ticket
    await saga.execute(
        tool="jira.create_issue", semantics=C,
        forward=lambda: jira.create_issue(project="ONB", summary="Onboard acme"),
        compensate=lambda r: Compensation(
            fn=close, handler="jira.close_issue",
            kwargs={"issue_key": r["key"]}))

    # the agent hallucinates a region here and raises →
    # PR closed, ticket closed, namespace dropped, instance terminated. LIFO.
```

The compensating action is a *factory over the forward result*, because you
cannot terminate an `instance_id` you have not seen yet — the same reason you
cannot refund a `charge_id` you have not seen yet.

---

## Spend and rate limits

A per-call threshold is not a spending control. `arg_exceeds("amount", 1000)`
inspects one call, so an agent issuing 1,000 charges of $999 satisfies it every
single time and moves $999,000. "No more than $50k a day" is a statement about a
*window*, and answering it requires state.

```python
from agent_saga import PreFlightGate, BudgetLimit, RateLimit, by_arg, combine, by_tool

gate = PreFlightGate(limits=[
    BudgetLimit("daily-spend", arg="amount", max_total=50_000, window=86_400),
    BudgetLimit("per-customer", arg="amount", max_total=1_000, window=86_400,
                scope=by_arg("customer_id")),
    RateLimit("velocity", max_calls=20, window=60, scope=by_tool),
    # Over budget doesn't have to mean refused — it can mean "ask a director".
    BudgetLimit("wire-ceiling", arg="amount", max_total=250_000, window=86_400,
                escalate_to_human=True),
])
```

The semantics that matter, all of them deliberate:

- **Limits are checked before rules.** A call already over budget is refused
  without spending a human's attention approving something that cannot proceed.
- **All-or-nothing.** A call refused by the third limit leaves the first two
  undebited.
- **A refusal hands the budget back** — refusal is the one outcome where we
  *know* the effect did not happen.
- **An authorization is permanent.** If the step then fails, the budget stays
  spent: a timed-out charge may well have reached the card network (the same
  position `STEP_UNKNOWN` takes). A compensated charge does not earn its budget
  back either, because an agent looping charge → refund → charge is precisely
  what a limit exists to stop. The meter measures **gross authorized outflow**,
  not net balance.
- **Fails closed.** An unreachable store, a limit that cannot read the amount it
  was told to police, or an exhausted budget with no approver all `BLOCK`. A
  limiter that passes calls through when its backend is down is not a limiter.

> **A local budget fails *open* across a fleet.** Unlike a lock, which merely
> fails to coordinate, ten pods with the default in-process store each grant the
> full allowance — the effective cap is 10×, silently. Set
> `set_limit_store(RedisLimitStore(...))` for anything running more than one
> process. The check-and-debit is a single Lua script, because GET-then-SET
> would let two nodes both read $49k, both decide $1k fits, and both spend.

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

Three reference connectors ship today. They are *worked examples* of the three
compensation classes, not the limit of what the engine covers — any tool call
you can write an inverse for works the same way (see
`examples/multi_domain.py`, which spans AWS, Pinecone, Jira, GitHub and Twilio
with no connector at all).

- **Stripe** (`COMPENSABLE`) — charge with a deterministic refund key; treats
  `charge_already_refunded` as success so a late-returning daemon doesn't loop.
- **PostgreSQL** (`COMPENSABLE`) — snapshots the affected columns in one
  autocommit round trip (never holds a transaction across the model's thinking
  time), and restores only if no concurrent writer touched the row.
- **Salesforce** (`COMPENSABLE`) — reverts only the patched fields, filtered to
  writable ones, guarded by `LastModifiedDate`.

Writing your own is the same three lines every time: pick the semantics, give
`compensate` a factory over the forward result, and register the handler by name
so `saga-recoveryd` can replay it after a crash.

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

## Framework adapters

Drop into an existing graph, crew, or agent without rewriting it. `wrap_tool`
keeps the original name, description, and args schema, so the model and the
router see no difference; the `saga_run` helper makes the whole run one
transaction.

```python
from agent_saga.adapters.langgraph import wrap_tool, saga_run
from agent_saga import ActionSemantics, Compensation

C = ActionSemantics.COMPENSABLE

# a DevOps agent: cluster + index + ticket, each with its inverse
safe_scale = wrap_tool(
    k8s_scale_deployment,                     # your existing @tool
    semantics=C,
    compensate=lambda r: Compensation(
        fn=scale_back, handler="k8s.scale_deployment",
        kwargs={"deploy": r["name"], "replicas": r["previous_replicas"]}))

safe_upsert = wrap_tool(
    qdrant_upsert_points, semantics=C,
    compensate=lambda r: Compensation(
        fn=delete_points, handler="qdrant.delete_points",
        kwargs={"collection": r["collection"], "ids": r["ids"]}))

safe_ticket = wrap_tool(
    zendesk_create_ticket, semantics=C,
    compensate=lambda r: Compensation(
        fn=close_ticket, handler="zendesk.close_ticket",
        kwargs={"ticket_id": r["id"]}))

# build the graph with the wrapped tools, then:
result = await saga_run(graph, {"messages": [...]})
# if any node raises, every tool that already ran is compensated LIFO
```

The same shape works for **CrewAI**, **AutoGen**, **LlamaIndex**, and the
**OpenAI Agents SDK** — see `agent_saga/adapters/`. AutoGen wraps a plain
callable, so it fits whichever of its tool APIs you are on.

```python
from agent_saga.adapters.autogen  import wrap_tool as autogen_tool
from agent_saga.adapters.crewai   import wrap_tool as crew_tool
from agent_saga.adapters.llamaindex import wrap_tool as llama_tool
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

Pre-alpha, by SagaOps. Implemented and tested (332 tests; the base suite runs
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
- Bounded over time on **both** backends: `FileWAL.compact()` rewrites the log
  keeping only unresolved sagas (atomic temp-file + `os.replace`, serialised
  against the flusher so a swap cannot lose a record), and
  `RecoveryDaemon.compact()` computes the keep-set for you with a grace period.
  A backend that cannot compact raises rather than silently no-opping.
- Bounded over time, not just correct on day one: a recovery sweep reads the
  log once (it used to re-read it per saga -- quadratic), `read_all()` pages in
  chunks, `read_since(gseq)` gives a cursor, the Redis ledger answers
  "already compensated?" from a SET/HASH index instead of scanning history, and
  `compact()` trims resolved records from the head of the log. Compaction never
  touches the completed-token index -- losing that would re-open the
  double-compensation window it exists to close.
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
