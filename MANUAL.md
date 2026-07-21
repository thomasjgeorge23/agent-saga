# agent-saga — User Manual

Version 0.1.0 · Apache-2.0 · SagaOps

This is the complete operating manual: what the system is, how every piece
works, what each module does, and how to run it in development and in
production. The [README](README.md) is the pitch; this is the reference.

---

## Table of contents

**Part I — Understanding it**
1. [What problem this solves](#1-what-problem-this-solves)
2. [The three ideas](#2-the-three-ideas)
3. [Install and first run](#3-install-and-first-run)
4. [Anatomy of one step](#4-anatomy-of-one-step)

**Part II — The building blocks**
5. [Semantics: REVERSIBLE / COMPENSABLE / IRREVERSIBLE](#5-semantics)
6. [Compensations and the registry](#6-compensations-and-the-registry)
7. [The transactional boundary](#7-the-transactional-boundary)
8. [The pre-flight gate](#8-the-pre-flight-gate)
9. [Spend and rate limits](#9-spend-and-rate-limits)
10. [The write-ahead log](#10-the-write-ahead-log)
11. [Tamper-evident audit](#11-tamper-evident-audit)
12. [Crash recovery](#12-crash-recovery)
13. [Snapshots](#13-snapshots)
14. [Isolation: tentative state and semantic locks](#14-isolation)

**Part III — Integrations**
15. [Connectors](#15-connectors)
16. [Framework adapters](#16-framework-adapters)
17. [The MCP proxy](#17-the-mcp-proxy)

**Part IV — Operating it**
18. [Time-travel debugger](#18-time-travel-debugger)
19. [Observability](#19-observability)
20. [Thread pools and tuning](#20-thread-pools-and-tuning)
21. [CLI reference](#21-cli-reference)
22. [Configuration reference](#22-configuration-reference)
23. [Deployment checklists](#23-deployment-checklists)
24. [Troubleshooting](#24-troubleshooting)
25. [Module map](#25-module-map)
26. [Known gaps](#26-known-gaps)

---

# Part I — Understanding it

## 1. What problem this solves

An LLM agent calls tools. Some of those tools do things to the real world:
charge a card, terminate an instance, open a ticket, write a row, send an
email. When the agent hallucinates at step 5, steps 1–4 have already happened.
Nothing in the agent framework knows how to take them back.

Retry does not help — the effects already landed. A try/except does not help —
the developer must hand-write the undo for every path. A workflow engine like
Temporal does not fully help either, because it makes you *declare the
compensating step at authoring time*, and the agent chose the action at
runtime.

`agent-saga` wraps each tool call, records a **runtime-derived inverse action**
before the effect happens, and unwinds the whole transaction in LIFO order —
in-process on failure, or from a separate recovery process if the process
itself is killed.

**What it is not.** It is not a retry library, not a workflow orchestrator, and
not a way to make irreversible actions reversible. Its most important behaviour
is *refusing* to start an uncompensable action without a human.

## 2. The three ideas

Everything in this codebase follows from three claims, listed in the order they
matter commercially.

### Idea 1 — "Undo" is not one thing

Every side effect is classified into one of three semantics. The classification
is mandatory; there is no default.

| | Meaning | Undo is | Examples |
|---|---|---|---|
| `REVERSIBLE` | State restores exactly. No observer can tell it happened. | a restore | in-process cache write, a scratch dict, an unapplied Terraform plan |
| `COMPENSABLE` | The effect can be semantically offset, but the trace is permanent. | a new, visible action | Stripe refund, terminate an EC2 instance, close a Jira ticket, delete a Slack message |
| `IRREVERSIBLE` | No automated action restores or offsets it. | nothing | SMS, email, wire transfer, `DROP TABLE` with no snapshot |

This is not vocabulary pedantry — it drives real behaviour:

- `REVERSIBLE` steps skip the fsync barrier (they die with the process, so
  there is nothing to recover).
- `COMPENSABLE` steps pay for two fsync barriers.
- `IRREVERSIBLE` steps are stopped by the gate *before* they run, and if one is
  found dangling after a crash, automated recovery halts for that whole saga.

### Idea 2 — The compensation is derived at runtime

You cannot refund a `charge_id` you have not seen yet. So compensations are
**factories over the forward result**:

```python
CompensationFactory = Callable[[Any], Optional[Compensation]]
```

The factory runs *after* the forward call returns, with the real result in
hand. That is the structural difference from a workflow engine, and it exists
because the agent — not a developer at authoring time — chose the action.

### Idea 3 — The pre-flight gate is the product

The rollback engine is the demo. The gate is the contract. It runs *before* any
side effect, which is the only point at which refusal is free. A bank does not
buy a post-disaster cleanup script; it buys a control that refuses to enter an
uncompensable boundary without a human on the hook.

## 3. Install and first run

**Requirements:** Python ≥ 3.10. The core has **zero dependencies**.

```bash
git clone <repo> && cd agent-saga
pip install -e .              # core, no third-party packages at all
pip install -e ".[dev]"       # + pytest for the test suite
```

Optional extras, installed only when you need that integration:

| Extra | Pulls in | For |
|---|---|---|
| `[langgraph]` | `langchain-core`, `langgraph` | LangGraph adapter |
| `[crewai]` | `crewai` | CrewAI adapter |
| `[openai-agents]` | `openai-agents` | OpenAI Agents SDK adapter |
| `[llamaindex]` | `llama-index-core` | LlamaIndex adapter |
| `[autogen]` | `autogen-core` | AutoGen adapter |
| `[postgres]` | `psycopg` | Postgres connector |
| `[stripe]` | `stripe` | Stripe connector |
| `[salesforce]` | `httpx` | Salesforce connector |
| `[encryption]` | `cryptography` | WAL-at-rest encryption |
| `[redis]` | `redis` | RedisWAL, RedisLedger, RedisLimitStore, RedisSemanticLocks |
| `[opentelemetry]` | `opentelemetry-api`, `opentelemetry-sdk` | OTel spans |
| `[all]` | everything above | |

### Run the demos — no network, no credentials

```bash
python examples/chaos_demo.py     # optimistic vs. transactional, side by side
python examples/multi_domain.py   # 5 systems (AWS/Pinecone/Jira/GitHub/Slack), 1 transaction
python examples/demo.py           # includes a simulated crash + recovery sweep
```

`multi_domain.py --fail-at 4` breaks a different step so you can watch a
different unwind.

### Run the test suite

```bash
python -m pytest -q
# 406 passed
```

The base suite runs on `pytest` alone. Tests for optional integrations skip
themselves when the SDK is absent.

### The smallest real program

```python
import asyncio
from agent_saga import saga_scope, ActionSemantics, Compensation

C = ActionSemantics.COMPENSABLE

async def main():
    async with saga_scope() as saga:
        ticket = await saga.execute(
            tool="jira.create_issue",
            semantics=C,
            forward=lambda: jira.create_issue(project="ONB", summary="Onboard acme"),
            compensate=lambda r: Compensation(
                fn=close_issue,
                handler="jira.close_issue",              # name, for crash recovery
                kwargs={"issue_key": r["key"]}),         # JSON-serializable
        )
        raise RuntimeError("the agent hallucinated a region")
        # -> ticket is closed automatically, then SagaAborted is raised

asyncio.run(main())
```

## 4. Anatomy of one step

This is the single most important sequence in the system. `SagaContext.execute`
([context.py:328](agent_saga/context.py#L328)) runs six phases in a fixed order,
and the order *is* the safety argument.

```
                        ┌─────────────────────────────────────────┐
  await saga.execute()  │                                         │
           │            │  1. GATE          limits, then rules    │
           ▼            │     ↓ refuse → PreFlightViolation       │
     ┌───────────┐      │       (nothing has happened yet)        │
     │  1 GATE   │      │                                         │
     └─────┬─────┘      │  2. STEP_INTENT → WAL                   │
           ▼            │     + fsync barrier if not REVERSIBLE   │
     ┌───────────┐      │                                         │
     │ 2 INTENT  │      │  3. FORWARD CALL (the real side effect) │
     │  + fsync  │      │     ↓ raises → STEP_UNKNOWN, re-raise   │
     └─────┬─────┘      │                                         │
           ▼            │  4. DERIVE the inverse from the result  │
     ┌───────────┐      │                                         │
     │ 3 FORWARD │      │  5. STEP_COMMITTED → WAL                │
     └─────┬─────┘      │     + second fsync barrier              │
           ▼            │                                         │
     ┌───────────┐      │  6. return result to the agent          │
     │ 4 DERIVE  │      │                                         │
     ├───────────┤      └─────────────────────────────────────────┘
     │ 5 COMMIT  │
     │  + fsync  │
     └─────┬─────┘
           ▼
       6 return
```

Why each detail is the way it is:

- **The gate runs first**, before anything. Refusal at any later point costs
  something; refusal here costs nothing.
- **Intent is written before the effect.** If the process dies during the
  forward call, the WAL still names what was about to happen, so the recovery
  daemon has something to act on. This is the whole reason it is a *write-ahead*
  log.
- **A raised forward call becomes `STEP_UNKNOWN`, not "did not happen."** A
  timed-out POST to Stripe may well have charged the card. The step stays on the
  rollback stack and compensation is still attempted, idempotently.
- **The second fsync is not redundant.** The compensation descriptor is only
  born *after* the forward call returns — it needed the result. A crash between
  the effect and that write leaves an intent with no way to undo it: exactly the
  orphan this engine exists to prevent. Set `durable_commit=False` on a
  `SagaContext` to trade this away for low-value work; never for payments.
- **`policy_args` exists because `forward_kwargs` is not trustworthy for
  policy.** A connector that wraps its call in a closure passes
  `forward_kwargs={}` — the amount is invisible to the gate. Declare
  policy-relevant values explicitly.

On failure, `rollback()` walks the stack **in reverse** (LIFO), because step N's
compensation may depend on state step N−1 created.

---

# Part II — The building blocks

## 5. Semantics

`agent_saga/semantics.py`

```python
from agent_saga import ActionSemantics, Compensation, SagaStep, StepState
```

### `ActionSemantics`

`REVERSIBLE` · `COMPENSABLE` · `IRREVERSIBLE` — described in §2.

**How to classify correctly.** The test is *"can another observer tell it
happened?"*

- A row update in a shared Postgres database is **COMPENSABLE**, not
  REVERSIBLE — other sessions read it, `ON UPDATE` triggers fired, `updated_at`
  moved, and CDC already shipped the intermediate state downstream.
- A file the saga owns on disk is **COMPENSABLE**, not REVERSIBLE — it survives
  a crash, so the undo must be crash-recoverable.
- An in-process dict is **REVERSIBLE** — it dies with the process, so a crash
  takes both the effect and the need to undo it.

Getting this wrong is not cosmetic: a REVERSIBLE step skips the fsync barrier,
so misclassifying a durable write makes it silently unrecoverable after a
crash.

### `StepState`

The lifecycle of one step, as recorded:

| State | Meaning |
|---|---|
| `INTENT_LOGGED` | Durable record written, not yet executed |
| `COMMITTED` | Forward call returned successfully |
| `UNKNOWN` | Forward call raised or timed out — the effect **may** have landed |
| `COMPENSATED` | Compensation ran successfully |
| `COMPENSATION_FAILED` | Compensation raised |
| `ORPHANED` | Executed, and no compensation exists |
| `UNRESOLVED` | Rollback halted before reaching this step |

`needs_compensation` is true for `COMMITTED` **and** `UNKNOWN`.

## 6. Compensations and the registry

### `Compensation`

```python
@dataclass(frozen=True)
class Compensation:
    fn: Callable            # what to call, in this process
    kwargs: dict            # its arguments
    description: str = ""
    idempotency_key: str    # defaults to a uuid; connectors override it
    handler: str | None     # name in the registry, for cross-process recovery
```

**The `recoverable` property is the one to understand.** A compensation can be
run by a *different process* reading only the WAL if and only if:

1. it names a `handler` registered in the compensation registry, **and**
2. its `kwargs` survive a JSON round trip (`json.loads(json.dumps(x)) == x`).

Both are checked when the compensation is created, not when it is needed — so
you learn a step is unrecoverable while everything is still fine, not at 3 a.m.
from a daemon that cannot fix it. The engine logs a warning (once per
tool+reason) when it sees an in-process-only compensation on a non-REVERSIBLE
step.

### The registry

`agent_saga/registry.py` — a closure cannot survive a `SIGKILL`.
`lambda: refund("ch_1")` is fine in-process and useless to a daemon that only
has the log.

```python
from agent_saga import compensator, registered, resolve

@compensator("stripe.refund")
def refund_charge(charge_id: str, amount: int,
                  idempotency_key: str, credential_ref: str) -> dict:
    ...
```

- Names must be **stable across deploys** — re-registering a name to a different
  function raises, because in-flight sagas would become unrecoverable.
- `registered()` lists every name; `resolve(name)` looks one up.
- **The recovery daemon must import the same modules as the agent.** If it does
  not, every dangling saga escalates with `handler not registered` — by design,
  not as a bug.

### Idempotency

`agent_saga/idempotency.py` — a compensation can run more than once (a daemon
retries, two daemons race, a process dies between doing the work and recording
it). Each must end with the effect applied exactly once.

```python
IdempotencyManager.key(saga_id, step_id, scope="compensate")
# -> SHA-256(f"{saga_id}:{step_id}:{scope}")[:32]
```

**The key deliberately does not include an attempt counter.** An idempotency key
works because the *downstream* system recognises a retry as a duplicate. If the
key changed per attempt, attempt 1 sends key A and attempt 2 sends key B, and
Stripe issues two refunds — the key would guarantee the double refund it was
meant to prevent. The attempt count lives in the ledger as telemetry instead.

Two layers, because either alone is insufficient:

1. **The key**, handed to the remote system, which de-duplicates on its side.
2. **The local execution ledger**, so a call already known to have succeeded is
   never issued at all — covering remotes with no idempotency support.

The key is auto-injected into any handler whose signature accepts
`idempotency_key` (or `**kwargs`). A connector-supplied key always wins, because
Stripe's refund key must match what the original request used.

## 7. The transactional boundary

`agent_saga/decorator.py`, `agent_saga/context.py`

There is exactly **one** boundary implementation. `@saga`, `saga_run`, and every
framework adapter route through `saga_scope`, so the begin/rollback/finish/lease
lifecycle lives in one place.

### `saga_scope` — the primary API

```python
from agent_saga import saga_scope

async with saga_scope(
    gate=None,                             # PreFlightGate; default gate if omitted
    wal=None,                              # AsyncWAL; a fresh in-memory one if omitted
    halt_on_compensation_failure=True,
) as saga:
    result = await saga.execute(...)
```

On any exception inside the scope: the cause is recorded (`SAGA_ABORT_CAUSE`),
compensations run LIFO, and `SagaAborted` is raised carrying the
`RollbackReport`.

### `@saga` — decorator form

```python
from agent_saga import saga

@saga                                       # requires an async function
async def onboard(customer_id): ...

@saga(reraise=False)                        # returns the RollbackReport instead
async def onboard(customer_id): ...
```

### `@tool` — register a saga-aware tool

```python
from agent_saga import tool, ActionSemantics, Compensation

@tool(semantics=ActionSemantics.COMPENSABLE,
      compensate=lambda r: Compensation(fn=close, handler="jira.close_issue",
                                        kwargs={"issue_key": r["key"]}))
async def create_issue(project: str, summary: str): ...
```

Outside a saga boundary the call passes through untouched, so the same tool
works in tests and one-off scripts. The wrapper accepts **keyword arguments
only**.

### `current_saga()`

Returns the active `SagaContext` for this async task, or `None`. Backed by
`contextvars`, not a global — concurrent agents in one process never share a
compensation stack.

### `SagaContext` — the full constructor

Used directly when you need the knobs `saga_scope` does not expose:

```python
SagaContext(
    gate=None,
    wal=None,
    halt_on_compensation_failure=True,   # stop unwinding after a failed compensation
    default_timeout=None,                # per-step timeout, seconds
    saga_id=None,                        # defaults to a uuid4 hex
    lease_ttl=5.0,                       # seconds; renewed at ttl/3
    durable_commit=True,                 # the second fsync barrier
)
```

Drive it manually with `await ctx.begin()` … `await ctx.finish()`, or let
`saga_scope` do it.

**`halt_on_compensation_failure=True` (the default) is deliberate.** If
compensating step N fails, step N−1's compensation may operate on state that is
no longer what it assumed. Continuing blindly is how a partial rollback becomes
a worse outcome than no rollback. Remaining steps are marked `UNRESOLVED` and
reported, never silently dropped.

**Leases, not PIDs.** From `begin()`, a background task appends a `SAGA_LEASE`
record every `lease_ttl / 3` seconds. This is how a recovery daemon tells "still
running" from "process is gone" — a PID would lie, because PIDs are reused
within minutes.

### `RollbackReport`

```python
report.compensated   # list[SagaStep] — undone successfully
report.failed        # compensation raised
report.orphaned      # executed, no compensation existed
report.unresolved    # rollback halted before reaching these
report.halted        # bool
report.clean         # not (failed or orphaned or unresolved)
report.summary()     # one-line verdict for an on-call engineer
```

`clean` is the only word that matters, and it is false more often than a naive
undo library admits.

### `SagaAborted`

```python
try:
    await onboard("acme")
except SagaAborted as e:
    e.cause      # the original exception
    e.report     # the RollbackReport
```

Callers must be able to distinguish "we cleaned up" from "we tried" — swallowing
that distinction is the failure mode this library exists to prevent.

## 8. The pre-flight gate

`agent_saga/gate.py`

```python
from agent_saga import (PreFlightGate, Rule, Verdict, GateContext,
                        PreFlightViolation, semantics_is, arg_exceeds, tool_is)
```

### Verdicts

- `ALLOW` — proceed.
- `REQUIRE_APPROVAL` — ask the approval provider; a denial becomes a BLOCK.
- `BLOCK` — raise `PreFlightViolation`. Nothing has happened yet.

### Evaluation order

```
limits (stateful, windowed)  →  rules (pure predicates, first match wins)
```

Rules are evaluated **in order** and the first match wins, so ordering encodes
precedence — put `BLOCK` rules first.

### The default rule

With no configuration you get exactly one rule:

```python
Rule(name="irreversible-requires-human",
     when=semantics_is(ActionSemantics.IRREVERSIBLE),
     verdict=Verdict.REQUIRE_APPROVAL,
     reason="Action cannot be undone or compensated by any automated means.")
```

With no `approval_provider` configured, `REQUIRE_APPROVAL` **becomes a BLOCK**.
That is the fail-closed default: an unapprovable action is a refused action.

### Built-in predicates

```python
semantics_is(ActionSemantics.IRREVERSIBLE, ...)   # match by semantics
tool_is("stripe.charge", "wire.send")             # match by tool name
arg_exceeds("amount", 1000)                       # one call's numeric argument
```

`arg_exceeds` sees exactly one call. It **cannot** express "no more than $50k a
day" — an agent issuing 1,000 charges of $999 satisfies it every time. For that
you need a limit (§9).

### A custom gate

```python
gate = PreFlightGate(
    rules=[
        Rule("no-prod-drops", tool_is("postgres.drop_table"), Verdict.BLOCK,
             "Schema changes are never agent-initiated."),
        Rule("big-charges", arg_exceeds("amount", 100_000),
             Verdict.REQUIRE_APPROVAL, "Charge over $1,000."),
        *DEFAULT_RULES,
    ],
    approval_provider=ask_a_human,     # sync or async: (ctx, rule) -> bool
    limits=[...],
)

async with saga_scope(gate=gate) as saga: ...
```

**A broken predicate fails closed.** If `rule.when(ctx)` raises, the call is
blocked, not allowed.

## 9. Spend and rate limits

`agent_saga/limits.py`

A per-call threshold is not a spending control. "No more than $50k a day" is a
statement about a *window*, and answering it requires state.

```python
from agent_saga import (PreFlightGate, BudgetLimit, RateLimit,
                        by_arg, by_tool, combine, set_limit_store, RedisLimitStore)

gate = PreFlightGate(limits=[
    BudgetLimit("daily-spend", arg="amount", max_total=50_000, window=86_400),
    BudgetLimit("per-customer", arg="amount", max_total=1_000, window=86_400,
                scope=by_arg("customer_id")),
    RateLimit("velocity", max_calls=20, window=60, scope=by_tool),
    BudgetLimit("wire-ceiling", arg="amount", max_total=250_000, window=86_400,
                escalate_to_human=True),      # over budget → ask a director
])
```

### Limit types

| | Caps | Fields |
|---|---|---|
| `BudgetLimit` | the **sum** of a numeric argument | `arg`, `max_total`, `window` |
| `RateLimit` | the **number of calls** | `max_calls`, `window` |

Both share: `name`, `window` (seconds), `scope`, `applies`,
`escalate_to_human`.

### Scopes — which bucket a call draws from

```python
GLOBAL                              # one shared bucket (default)
by_tool                             # a bucket per tool name
by_arg("customer_id")               # a bucket per argument value
combine(by_tool, by_arg("region"))  # intersect dimensions
```

A call that omits a `by_arg` dimension lands in one shared "missing" bucket
rather than each getting a private unlimited one — a tool that forgets to
declare the dimension is throttled together instead of escaping the limit.

### The semantics that matter

- **Limits are checked before rules.** A call already over budget is refused
  without spending a human's attention on approving something that cannot
  proceed.
- **All-or-nothing.** A call refused by the third limit leaves the first two
  undebited.
- **A refusal hands the budget back.** Refusal is the one outcome where we
  *know* the effect did not happen.
- **An authorization is permanent.** If the step then fails, the budget stays
  spent — a timed-out charge may well have reached the card network. A
  *compensated* charge does not earn its budget back either, because an agent
  looping charge → refund → charge is precisely what a limit exists to stop. The
  meter measures **gross authorized outflow**, not net balance.
- **Fails closed.** An unreachable store, a limit that cannot read the amount it
  was told to police, an exhausted budget with no approver — all `BLOCK`. A
  limiter that passes calls through when its backend is down is not a limiter.
- **Negative amounts raise.** Model refunds as their own tool, not as a negative
  charge, or an agent could mint allowance by alternating signs.
- **`bool` is not a number.** `charge(amount=True)` does not read as 1.

### Stores

| Store | `distributed` | Notes |
|---|---|---|
| `InProcessLimitStore` | `False` | Default. Sliding *log* of `(timestamp, amount)`, not a counter — exact at the window edge and itemizable when a risk officer asks what made up the $47k. |
| `RedisLimitStore` | `True` | One window shared across every node. Check-and-debit is a single Lua script. |

> ⚠️ **A local budget fails *open* across a fleet.** Unlike a lock, which merely
> fails to coordinate, ten pods with the default in-process store each grant the
> full allowance — the effective cap is 10×, silently. Set a shared store for
> anything running more than one process:
>
> ```python
> set_limit_store(RedisLimitStore("redis://localhost:6379/0"))
> ```
>
> The check-and-debit must be atomic, because GET-then-SET would let two nodes
> both read $49k, both decide $1k fits, and both spend.

## 10. The write-ahead log

`agent_saga/wal/`

The engine's entire safety property is *intent is durable before the side
effect*, and exactly one call enforces it: `barrier()`.

### The interface — `BaseWAL`

```python
async def start()                       # open the sink, begin flushing
async def close()                       # drain, then release
def     append(event, payload) -> int   # SYNCHRONOUS by contract; returns seq or DROPPED
async def barrier(seq=None)             # return only when everything ≤ seq is durable
async def read_all() -> list[dict]
async def clear()
async def compact(*, keep_saga_ids)     # raises if unsupported — never a silent no-op
async def ensure_capacity()             # yield until the buffer has room (BLOCK policy)
```

`append` is synchronous on purpose: it sits between an agent and every tool
call, and making it a coroutine would add an event-loop hop to each one.

`barrier()` is a first-class part of the contract, not an extra. **A backend
without a durability fence is fire-and-forget**, and a crash against it orphans
real charges with no record to recover from.

### The durability tiers

| Path | What it costs | Survives |
|---|---|---|
| `append()` | lock-free deque push, sub-microsecond | a caught exception |
| `barrier()` | flush + fsync, a real disk round trip | `SIGKILL` |

You pay for the barrier only where losing the record is unacceptable: before any
`COMPENSABLE` or `IRREVERSIBLE` effect. `REVERSIBLE` steps ride the fast path.
That tiering is why the hot-path overhead is defensible.

### Group commit

Concurrent barriers landing in the same flush window share **one** fsync. This
falls out of batching for free, and it is why throughput scales roughly linearly
with concurrency (measured 300 → 33k ops/s from 1 → 256 concurrent sagas) while
p99 stays bounded.

Instrumentation: `wal.barriers`, `wal.flush_cycles` — their ratio is the
amortization factor. `wal.dropped` is non-zero only under `DROP_SILENT`.

### Backpressure

```python
from agent_saga import BackpressurePolicy, FileWAL

FileWAL("./agent-saga.wal", max_buffer=100_000,
        backpressure=BackpressurePolicy.RAISE)     # the default
```

| Policy | Behaviour |
|---|---|
| `RAISE` **(default)** | raise `WALBackpressure` *before* the side effect, so the step is safely abortable |
| `BLOCK` | `ensure_capacity()` yields until there is room; never loses a record |
| `DROP_SILENT` | sheds the record and increments `dropped` |

A silently dropped record is a silently unrecoverable side effect. That is why
`RAISE` is the default.

### No unbounded waits

`barrier()` raises `WALStalled` after `barrier_timeout` (30 s default) rather
than hanging forever on a wedged device. A sink error fails every pending fence
*immediately*, with the real cause, so a caller learns why durability failed
while it is still before the side effect. `close()` is bounded too.

### Backends

**`FileWAL`** (aliased `AsyncWAL`) — the zero-dependency default. Append-only
JSON-lines, fsync-durable, one private single-thread flusher per WAL.
`path=None` keeps everything in memory (tests, all-REVERSIBLE sagas).

```python
FileWAL(path=None, *, max_buffer=100_000, backpressure=RAISE,
        encryptor=<from env>, barrier_timeout=30.0, chain=True)
```

`compact(keep_saga_ids=...)` rewrites the log keeping only listed sagas plus all
chain attestations. It is safe because the io-lock is held (no flush can race
it), survivors go to a temp file that is fsynced then `os.replace`d (atomic on
POSIX and Windows), and appends during compaction land in the in-memory buffer.

**`RedisWAL`** — one shared log across every pod, so a daemon on node B can
recover a saga orphaned on node A.

```python
from agent_saga.wal import RedisWAL
RedisWAL("redis://localhost:6379/0", key="agent-saga:wal",
         wait_replicas=0, wait_timeout_ms=1000)
```

> ⚠️ **Read this before putting money through it.** Redis is not, by default, a
> durable log. With the usual `appendfsync everysec` it acknowledges a write and
> can lose it up to a second later; a failover can lose writes the primary
> already acknowledged. So `barrier()` here means *"Redis acknowledged, and if
> `wait_replicas` is set, that many replicas acknowledged too"* — a weaker
> guarantee than fsync-to-disk, and the class does not pretend otherwise. For a
> financial ledger: `appendfsync always` **and** `wait_replicas>=1`, or keep
> `FileWAL` on a durable volume.

`RedisWAL` stamps a **global sequence** (`gseq`) from a shared counter (one
`INCRBY` per batch) so records on a multi-node log are uniquely identified and
globally ordered; the per-process `seq` stays intact for fence bookkeeping. It
also offers `read_since(gseq)` as a cursor and pages `read_all()` in chunks.

### Encryption at rest (BYOK)

`agent_saga/encryption.py` · `pip install agent-saga[encryption]`

The WAL holds real business data — row snapshots, API arguments, compensation
payloads. On a shared or regulated host that plaintext is a liability.

```python
from agent_saga import generate_key, FernetEncryptor, set_wal_encryptor

print(generate_key())                       # mint one
set_wal_encryptor(FernetEncryptor(key))     # or set AGENT_SAGA_WAL_KEY
```

Each line is either raw JSON (plaintext) or `E1:<fernet-token>`. The per-line
prefix lets a reader auto-detect, tolerate a log that was plaintext before a key
was introduced, and give a precise error on an encrypted line it cannot read.

**Fail loud, never silent.** A reader or daemon that meets an encrypted record
without a key errors rather than skipping it — a daemon that treated an
unreadable WAL as "no work to do" would abandon every crashed saga in it.

The `cryptography` package is imported lazily and only when a key is configured.
No key set → nothing imported, WAL stays plaintext.

### WAL event vocabulary

Every record carries `seq`, `event`, `ts`, plus chain fields, plus the payload.

| Event | Written when |
|---|---|
| `SAGA_START` | boundary opens (carries `pid`, `lease_ttl`) |
| `SAGA_LEASE` | heartbeat, every `lease_ttl/3` |
| `STEP_INTENT` | before a forward call |
| `STEP_COMMITTED` | forward returned; carries the compensation descriptor |
| `STEP_UNKNOWN` | forward raised/timed out; effect may have landed |
| `SAGA_ABORT_CAUSE` | the exception type + message that triggered rollback |
| `ROLLBACK_START` / `ROLLBACK_END` | unwinding brackets |
| `COMPENSATED` | one compensation succeeded |
| `COMPENSATION_FAILED` | one compensation raised |
| `STEP_ORPHANED` | executed with no compensation available |
| `SAGA_COMPLETE` / `SAGA_ABORTED` | terminal record |
| `TENTATIVE_REGISTERED` / `TENTATIVE_RESOLVED` / `TENTATIVE_UNRESOLVED` | tentative-resource lifecycle |
| `WAL_CHAIN_GAP` | compaction attestation |

## 11. Tamper-evident audit

`agent_saga/integrity.py`

A WAL is already the record of what an agent did with real money. Chained, it
becomes something an auditor can rely on: every record commits to its
predecessor, so any edit, reorder, insertion, or deletion invalidates the chain
and the verifier names the first record that stops adding up.

**On by default** (`chain=True`) — one SHA-256 per record, computed on the
flusher thread, off the caller's hot path. A log that is only *sometimes*
chained is not evidence of anything.

### The format

```
_s   salt        16 random bytes, hex
_cd  digest      sha256(salt_ascii || canonical_json(business_fields))
_ph  prev hash   the predecessor's _h, stored not implied
_h   hash        sha256(canonical_json({prev, seq, ts, event, cd}))
```

**The chain never hashes the payload directly.** It hashes a *salted content
digest*, and that indirection is what makes the two legitimate mutations
possible without weakening the proof.

### Legitimate mutation 1 — GDPR erasure

```python
from agent_saga import redact_record, redact_where

records, n = redact_where(records, lambda r: r.get("customer_id") == "cus_42")
```

`redact_record` destroys the payload **and the salt**, keeps `seq/ts/event/_h/
_cd/_ph`, and marks `_redacted`. The chain still verifies end to end. What
survives is proof that a record existed, when, in what order, and of what type —
with its contents irrecoverable, including by whoever holds the log.

Dropping the salt is load-bearing: `{"amount": 4200}` has few plausible
preimages, so an unsalted digest would leak the value it was meant to erase.

### Legitimate mutation 2 — compaction

`compact()` legitimately drops settled sagas, which looks *exactly* like an
attacker deleting the record of a charge — both are missing sequence numbers.

So compaction appends a **chained attestation** (`WAL_CHAIN_GAP`) naming
precisely which sequence ranges left and the digest of what they were. A gap is
accepted only when an attestation accounts for every sequence inside it.
Attestations survive later compactions, because housekeeping that erased them
would turn an explained gap back into an apparent attack.

It is *appended*, not spliced in at the gap, because splicing would require
re-hashing every record after it — precisely the power a tamper-evident log must
not hand out.

### Verifying

```bash
agent-saga verify --wal-path ./agent-saga.wal          # exit 0 only if intact
agent-saga verify --wal-path ./agent-saga.wal --strict # also fail on unchained records
```

Designed to be a CI gate and a cron job — the exit code is the product, the
report is for the human reading the failure. `--strict` refuses records written
before chaining was enabled; the default tolerates them so a pre-existing log
still verifies from the point chaining began.

The verifier checks three things independently, which is what lets one break
produce one finding instead of cascading:

1. **Content** — the payload matches its own digest (catches an altered field).
2. **Self-consistency** — the record's hash follows from the predecessor it
   *claims* (catches an edited header or an edited `_ph`).
3. **Linkage** — the predecessor it claims is the record actually before it
   (catches insertion, reorder, unattested deletion).

### WORM export

```bash
agent-saga export --wal-path ./agent-saga.wal --out ./audit-2026-07
```

Writes `records.jsonl` plus `manifest.json` carrying the chain head, the
bundle's own SHA-256, and **the verification rule in prose** — so an auditor can
re-check it years from now with `sha256sum` and nothing else. An archive
readable only by the tool that wrote it is not evidence, it's a dependency.

Export refuses a broken chain unless you pass `--allow-broken`, which labels it.
Exporting a broken chain silently would launder it into an artifact that looks
authoritative.

> **Scope.** The chain proves *one writer's* log is intact, and is per-process by
> construction: a single chain across nodes would need a global lock on every
> append — a distributed transaction on the hot path of every tool call. For a
> fleet, each node's log is independently provable; correlating them is a
> control-plane concern.

## 12. Crash recovery

`agent_saga/recovery.py`

A write-ahead log nobody reads is just an audit file. If a process is `SIGKILL`ed
after a charge's intent is durable, that charge is orphaned until an independent
process resolves it.

### Design commitments

1. **Fail closed.** Anything the daemon cannot resolve with certainty is
   escalated to a human queue, never guessed at. An `IRREVERSIBLE` step anywhere
   in a dangling saga halts automated recovery for that saga entirely.
2. **Leases, not PIDs.** Only an expired lease proves the owner is gone — and
   with a 2× TTL grace, so a GC pause or a stalled disk is not mistaken for a
   dead process. (A false positive here causes double-compensation.)
3. **Deterministic tokens.** Two daemons racing on the same WAL derive identical
   recovery tokens, so the second sees the first's ledger entry and declines.
   Double-refunds are structurally impossible, not merely unlikely.

### Running it

> **Note:** `saga-recoveryd` is the *name* of this component in the docs. There
> is no installed binary — the only console script is `agent-saga`. Run the
> daemon as a small Python program:

```python
# recoveryd.py
import asyncio, logging
from agent_saga import RecoveryDaemon

# CRITICAL: import the same connector/handler modules the agent imports, or
# every dangling saga escalates with "handler not registered".
import agent_saga.connectors.stripe          # noqa: F401
import my_app.tools                          # noqa: F401

logging.basicConfig(level=logging.INFO)
daemon = RecoveryDaemon("./agent-saga.wal", dry_run=True)   # start in dry-run
asyncio.run(daemon.watch(interval=5.0))
```

Point it at production only after watching it narrate what it *would* do for a
week — `dry_run=True` journals `RECOVERY_DRY_RUN` and touches nothing.

### Constructor

```python
RecoveryDaemon(
    wal_path,                  # a path, OR any BaseWAL (so a daemon on node A
                               # can recover a saga orphaned on node B)
    journal_path=None,         # default: <wal>.recovery.jsonl
    claims_dir=None,           # default: <wal-dir>/.claims
    daemon_id=None,
    dry_run=False,
    lock=None,                 # RecoveryLock; default FileLock
    ledger=None,               # RecoveryLedger; default FileLedger
)
```

### The sweep

```
read the log ONCE  →  fold into per-saga state (parse_wal)
                   →  select dangling sagas
                   →  read the ledger ONCE
                   →  resolve each
```

`recover_all()` hoists both reads. An earlier version re-read the whole WAL per
saga — quadratic, which on a long-lived log degrades into an outage.

For each dangling saga, in order:

| Check | Outcome |
|---|---|
| Lease still being renewed? | `SKIPPED_ACTIVE` — the owner is alive |
| Nothing pending? | `NOTHING_TO_DO` |
| Any `IRREVERSIBLE` step pending? | `NEEDS_HUMAN` — halt before touching anything |
| Another daemon holds the claim? | `SKIPPED_CLAIMED` |
| Token already in the completed set? | skip this step |
| No compensation recorded, or not `recoverable`? | `NEEDS_HUMAN` |
| Handler not registered in *this* process? | `NEEDS_HUMAN` |
| Compensation raises? | `NEEDS_HUMAN`, halted |
| All steps compensated LIFO | `RECOVERED` |

Then stranded tentative resources are rolled back, in the same order an
in-process rollback would use.

Every attempt is journalled **before** it acts — the same write-ahead rule the
agent follows, so a daemon that crashes mid-compensation leaves evidence it
tried.

`INTENT_LOGGED` with no terminal record is the nastiest case: the process died
somewhere around the network call, so we cannot know whether the effect landed
and **must assume it did**.

### The completed-token set

Read from **both** sources:

- the **ledger** — what recovery did, possibly on another node;
- the **crashed process's own WAL** — whose `COMPENSATED` records prove work no
  daemon ever did.

A daemon that consulted only its own journal would re-run a compensation the
dead process had already finished.

### Ledgers

`agent_saga/ledger.py`

| Ledger | `distributed` | Use with |
|---|---|---|
| `FileLedger` | `False` | `FileWAL`, one host |
| `InMemoryLedger` | `False` | tests, embedded daemons |
| `RedisLedger` | `True` | any shared WAL |

> ⚠️ **A node-local ledger behind a shared WAL is a double-compensation bug.**
> Two nodes derive the same idempotency key, neither finds the other's
> `RECOVERY_SUCCESS`, and both run the compensation. Where the remote honours the
> key that is merely wasteful; where it does not, it is a double refund. The
> daemon **warns** when it detects this combination — do not ignore it.

### Locks

`agent_saga/locks.py`

| Lock | Notes |
|---|---|
| `FileLock` (default) | one claim file per key, `O_EXCL`, atomic on local filesystems |
| `InProcessLock` | for an embedded daemon or tests |

Deliberately **no distributed backend ships in-tree** for the recovery claim —
adding Redis as a core dependency would cost every single-node user the
zero-setup path. Inject any object implementing `acquire(key) -> bool` /
`release(key)`.

Idempotency does not depend on the lock. Deterministic tokens plus the ledger
are what make double-compensation impossible; the lock is an efficiency and
tidiness guard on top. **A weaker distributed lock costs throughput, never
safety.** A filesystem lock over NFS is not trustworthy — inject a real one for
a multi-host fleet.

### Compaction

```python
removed = await daemon.compact(grace_seconds=3600.0)
```

Computes the keep-set for you: a saga is kept if it is unresolved, has stranded
tentatives, or resolved only recently. Works on any backend. Compaction never
touches the completed-token index — losing that would re-open the
double-compensation window it exists to close.

## 13. Snapshots

Two different kinds of "private" state, with genuinely different requirements.

### In-process — `reversible()`

`agent_saga/snapshot.py` — state that dies with the process: a dict the agent is
assembling, an in-memory object, a scratch structure nobody else observes.

```python
from agent_saga import reversible

cart = {"items": [], "total": 0}
await reversible(ctx, target=cart,
                 mutate=lambda c: c.update(items=["sku_1"], total=42))
# on rollback, cart is exactly {"items": [], "total": 0} again
```

Two properties fall out of capturing *before* the forward call:

1. **Restore is valid even on an UNKNOWN outcome.** The inverse was fully
   determined before the mutation ran, so it is correct whether the mutation
   half-applied, fully applied, or raised.
2. **It is legitimately REVERSIBLE**, so it rides the WAL fast path. The
   justification is correctness, not performance: in-process state does not
   survive a crash, so there is no orphan to recover.

Strategies (`auto_strategy` picks one from the target's shape):

| Strategy | Target |
|---|---|
| `MappingSnapshot` | dict-like — **clear-and-repopulate**, so a key the mutation *added* is removed on restore |
| `SequenceSnapshot` | list-like, restored in place so aliases see it |
| `SetSnapshot` | set-like |
| `AttributeSnapshot([...])` | named attributes of an object; everything else untouched |

Immutable targets (`str`, `bytes`) raise — snapshot the object that holds them.

### Durable — `snapshot_file()`

`agent_saga/durable.py` — state private to the saga that **survives a crash**: a
config file the agent rewrites, a generated artifact.

```python
from agent_saga import snapshot_file
from pathlib import Path

await snapshot_file(ctx, path="config.yaml",
                    mutate=lambda p: Path(p).write_text(new_yaml))
```

Three design changes from the in-process version, each forced by durability:

1. **COMPENSABLE, not REVERSIBLE.** A crash leaves the file on disk, so the undo
   must be a registry-backed handler (`durable.restore_file`) the daemon can run.
2. **The bytes go to a store; only a reference goes in the WAL.** A 10 MB file's
   prior contents must never be fsynced into the log. Same rule as credentials:
   the WAL carries a pointer.
3. **A guard.** Restore verifies the file still holds what the saga wrote and
   raises `StaleFile` if someone edited it in the meantime, rather than
   clobbering their change.

`existed=False` means the saga created the file, so the undo is to delete it.

**The snapshot store** must be reachable by both the agent and the daemon.

```python
from agent_saga import FileSnapshotStore, set_snapshot_store
set_snapshot_store(FileSnapshotStore("/shared/agent-saga-snapshots"))
# or set AGENT_SAGA_SNAPSHOT_DIR (default: .agent_saga_snapshots)
```

### Garbage collection

`agent_saga/gc.py` — a durable snapshot exists only to undo a saga. Once the
saga is resolved, it is dead weight.

```python
from agent_saga import SnapshotGC

gc = SnapshotGC("./agent-saga.wal",
                recovery_journal="./agent-saga.recovery.jsonl",
                grace_seconds=3600.0, dry_run=True)
report = gc.collect()
print(report.summary())
```

Built around one failure it must never cause — deleting a snapshot a rollback
still needs. Three guards:

1. A snapshot referenced by an **unresolved** saga is always kept.
2. A snapshot referenced by a saga that **escalated to a human** is kept; the
   operator resolving it by hand may need it.
3. A **grace period** — even a resolved saga's snapshots are kept until its last
   WAL activity is older than `grace_seconds`.

Anything the sweep is unsure about, it keeps.

## 14. Isolation

A saga has **no ACID isolation** — every step commits as it runs. The classic
failure: step 1 debits an account, step 3 fails, and in between a second reader
saw money that was about to come back. You cannot fix that with a database
transaction, because holding one across an LLM's thinking time is exactly what
the saga pattern exists to avoid.

Two structural countermeasures, both wired into the lifecycle so they cannot be
forgotten.

### Tentative state

`agent_saga/patterns/tentative.py` — stop pretending the write is final.

```python
from agent_saga import tentative

balance = tentative(saga, "account:usr_123",
                    on_commit=confirm_debit,
                    on_rollback=restore_balance,
                    rollback_handler="ledger.restore_balance",   # for crash recovery
                    rollback_kwargs={"account": "usr_123", "amount": 500},
                    lock=True)
```

Status is `PENDING` → `COMMITTED` | `ROLLED_BACK`, resolved **exactly once** by
the saga boundary. No caller has to remember to do it, which is the point: the
failure path is the one people forget.

Transitions are **enforced, not advisory** — a resource cannot go from
`COMMITTED` back to `PENDING`, and cannot be resolved twice
(`TentativeConflictError`). A double resolution usually means a lifecycle bug
that would otherwise surface much later as a mysteriously wrong balance.

Registration is written to the WAL (`TENTATIVE_REGISTERED`), not just held in
memory — a resource that lived only in this process would be stranded `PENDING`
forever by a `SIGKILL` with no daemon able to see it. **Supply
`rollback_handler` and JSON-serializable `rollback_kwargs`**, or the engine warns
that a crash leaves it unrecoverable and the daemon will escalate rather than
guess.

`register_tentative_durable()` adds a fence — use it when the tentative write is
the money and the registration must be on disk *before* the debit.

### Semantic locks

`agent_saga/locks.py` — claim a *business* resource, not a database row (which
would reintroduce the long-held-transaction problem).

```python
await ctx.acquire_semantic_lock("account:usr_123", timeout=0.0)
```

- **Re-entrant per saga** — a multi-step workflow naturally touches one account
  more than once.
- **`timeout=0` fails fast**, which is usually right: telling an agent the
  account is busy beats freezing it mid-run behind another saga. Raises
  `SemanticLockConflictError` *before* the step runs — the same stance as the
  gate.
- **Released on every exit path**, including abort. An aborted saga must not
  strand a resource claimed forever.

| Manager | `distributed` | |
|---|---|---|
| `SemanticLockManager` (default) | `False` | in-memory dict; correct in one process |
| `RedisSemanticLocks` | `True` | `pip install agent-saga[redis]` |

```python
from agent_saga import set_semantic_locks, RedisSemanticLocks
set_semantic_locks(RedisSemanticLocks("redis://localhost:6379/0", ttl_ms=30_000))
```

Three details carry `RedisSemanticLocks`' correctness:

- **Acquire is `SET key token NX PX ttl`** — atomic test-and-set with an expiry,
  so a holder that is SIGKILLed releases the resource when the TTL lapses instead
  of deadlocking it forever.
- **Release is a compare-and-delete Lua script**, never a bare `DEL`. If our TTL
  lapsed and another saga took the lock, a bare `DEL` would free *their* claim —
  the classic distributed-lock bug, and a silent one.
- **Renewal extends the TTL** while the saga is alive (at ttl/3), so a long agent
  run does not lose its lock mid-transaction. The TTL is a crash-detector, not a
  deadline on the work.

Note: a distributed manager cannot be acquired synchronously (it is a network
round trip), so `tentative(..., lock=True)` raises against one and tells you to
`await ctx.acquire_semantic_lock(...)` first with `lock=False`. A lock that lies
is worse than no lock.

---

# Part III — Integrations

## 15. Connectors

`agent_saga/connectors/`

Three reference connectors ship today. They are *worked examples* of the
compensation classes, not the limit of what the engine covers — see
`examples/multi_domain.py`, which spans AWS, Pinecone, Jira, GitHub and Twilio
with no connector at all.

### Credentials never enter the WAL

Compensation kwargs are fsynced in plaintext and read by another process, so
connectors pass a credential **reference**, resolved from the daemon's own
secret store at use time.

```python
from agent_saga.connectors import set_credential_resolver
set_credential_resolver(lambda ref: vault.read(f"agents/{ref}"))
# fallback with no resolver: env var AGENT_SAGA_CRED_<REF_UPPERCASED>
```

`assert_no_secrets(kwargs, where=...)` raises `SecretLeak` at authoring time if
a secret slips into compensation kwargs. This is not defense in depth — it is
the difference between a WAL you can hand to an auditor and one you must treat
as a secret-bearing artifact forever.

### Stripe — `COMPENSABLE`

```python
from agent_saga.connectors.stripe import charge

result = await charge(ctx, customer_id="cus_1", amount=4200,
                      credential_ref="stripe_prod", currency="usd")
```

- `amount` is in the smallest currency unit (cents). Passing dollars charges
  100× — exactly what an `arg_exceeds("amount", ...)` rule is for.
- Forward idempotency scoped to saga+customer+amount, so an agent retrying its
  own tool call does not double-charge.
- The refund key is **deterministic from the charge id**, so a daemon derives the
  same key without it being recorded anywhere.
- `charge_already_refunded` is treated as **success**. Stripe retains idempotency
  keys for 24 h; a daemon returning after a two-day outage gets a fresh key and
  would otherwise loop forever on an error that means "already done."
- An `UNKNOWN` outcome returns **no compensation** — there is no charge id to
  refund by. It logs the idempotency key to reconcile against, and the step is
  reported ORPHANED rather than silently "cleaned."

### PostgreSQL — `COMPENSABLE`

```python
from agent_saga.connectors.postgres import update_row, insert_row, delete_row

await update_row(ctx, pool=pool, table="accounts", pk_column="id", pk_value=7,
                 updates={"status": "active"}, credential_ref="pg_main")
await insert_row(ctx, pool=pool, table="accounts", values={...},
                 pk_columns=["id"], credential_ref="pg_main")
await delete_row(ctx, pool=pool, table="accounts", pk={"id": 7},
                 credential_ref="pg_main")
```

- **COMPENSABLE, not REVERSIBLE** — other sessions read the mutated row, `ON
  UPDATE` triggers fired, `updated_at` moved, CDC shipped the intermediate state
  downstream where it may already have fired a webhook you cannot recall.
- Snapshot and mutation happen in **one short autocommit round trip**. No
  transaction is held across the model's thinking time — that would pin a
  connection and hold row locks for however long the model takes.
- Only the affected columns are snapshotted; `SELECT *` would drag in generated
  and identity columns that are not writable on restore.
- Restore is guarded — if a concurrent writer touched the row,
  `ConcurrentModification` is raised rather than silently discarding their write.
- **Identifiers are validated against an allowlist and quoted, never
  interpolated raw.** An LLM chooses table and column names at runtime, so this
  is a live injection surface in a way it is not in ordinary apps.
- Compound primary keys are supported throughout.

### Salesforce — `COMPENSABLE`

```python
from agent_saga.connectors.salesforce import patch_object

await patch_object(ctx, instance_url="https://acme.my.salesforce.com",
                   object_type="Account", object_id="001...",
                   patch={"Phone": "555-0100"}, credential_ref="sf_prod")
```

Reverts **only the patched fields**, filtered to writable ones, guarded by
`LastModifiedDate`.

### Writing your own

The same three lines every time:

1. Pick the semantics honestly.
2. Give `compensate` a factory over the forward result.
3. Register the handler by name with JSON-serializable kwargs so recovery can
   replay it after a crash.

```python
from agent_saga import compensator, ActionSemantics, Compensation

@compensator("qdrant.delete_points")
def delete_points(collection: str, ids: list, idempotency_key: str = "") -> dict:
    ...

async def upsert(ctx, *, collection, points):
    return await ctx.execute(
        tool="qdrant.upsert",
        semantics=ActionSemantics.COMPENSABLE,
        forward=lambda: client.upsert(collection, points),
        compensate=lambda r: Compensation(
            fn=delete_points, handler="qdrant.delete_points",
            kwargs={"collection": collection, "ids": r["ids"]}),
        policy_args={"collection": collection, "count": len(points)},
    )
```

## 16. Framework adapters

`agent_saga/adapters/`

Drop into an existing graph, crew, or agent without rewriting it. A wrapped tool
keeps its **name, description, and args schema**, so the model and the router
see no difference.

All five adapters share one routing core (`_common.build_runner`): inside a saga
it routes through `SagaContext.execute` with the tool's arguments passed as
`policy_args` (so threshold rules can actually see them); outside a saga it calls
the tool untouched, so the same wrapped tool works in a unit test.

| Framework | Module | Wrap | Run |
|---|---|---|---|
| LangGraph / LangChain | `adapters.langgraph` | `wrap_tool` (BaseTool or plain fn) | `saga_run(graph, input)` |
| CrewAI | `adapters.crewai` | `wrap_tool` (sync `_run`, bridged to a worker thread) | `saga_kickoff(crew, ...)` |
| OpenAI Agents SDK | `adapters.openai_agents` | `wrap_tool` (parses the JSON argument string so the gate sees real values) | `saga_run(...)` |
| LlamaIndex | `adapters.llamaindex` | `wrap_tool` (uses `.async_fn` / `.fn`) | `saga_run(...)` |
| AutoGen | `adapters.autogen` | `wrap_tool` (plain callable — fits whichever tool API you are on) | `saga_run(...)` |

```python
from agent_saga.adapters.langgraph import wrap_tool, saga_run
from agent_saga import ActionSemantics, Compensation

C = ActionSemantics.COMPENSABLE

safe_scale = wrap_tool(
    k8s_scale_deployment,                     # your existing @tool
    semantics=C,
    compensate=lambda r: Compensation(
        fn=scale_back, handler="k8s.scale_deployment",
        kwargs={"deploy": r["name"], "replicas": r["previous_replicas"]}))

# build the graph with the wrapped tools, then:
result = await saga_run(graph, {"messages": [...]})
# if any node raises, every tool that already ran is compensated LIFO
```

`saga_run` accepts `gate=`, `wal=`, `halt_on_compensation_failure=`,
`reraise=False` (return the report instead of raising), and `invoke=` — a
coroutine `(saga_context) -> result` for `astream`, custom configs, or
interleaving your own steps.

**Propagation** works through `contextvars`: LangGraph runs tools in the same
context (async tasks copy it; sync tools go through a context-preserving
executor), so wrapped tools see the saga without threading anything through
graph state.

## 17. The MCP proxy

`agent_saga/mcp/`

Wrapping tools asks the agent's author to refactor the thing they're already
nervous about. An MCP client talks to servers over a socket, so a proxy can sit
in that socket and give the same guarantees to an agent that has no idea it's
there.

### The adoption ramp

```bash
# 1. Learn what your agent actually calls, changing nothing.
agent-saga mcp --observe --emit-policy saga-policy.json -- python -m my_mcp_server

# 2. Classify what it found, then enforce.
agent-saga mcp --policy saga-policy.json -- python -m my_mcp_server
```

**Undeclared tools are refused** in enforce mode. Not allowed-with-a-warning: if
nobody has said whether a tool can be undone, it doesn't reach a real system.

Observe mode is the ramp — it forwards everything, records the real tool surface,
and emits a skeleton. **Every entry comes back `IRREVERSIBLE` with a TODO,
deliberately**: a generator that guessed `COMPENSABLE` and invented an inverse
would be asserting that a real financial operation is undoable on the evidence
of a tool *name*, which is the one guess this project exists to refuse.
Reviewers downgrade what's safe; the file never upgrades itself.

### The policy file

```json
{
  "mode": "enforce",
  "unknown_semantics": null,
  "tools": {
    "stripe__create_charge": {
      "semantics": "COMPENSABLE",
      "compensate": {
        "tool": "stripe__create_refund",
        "args":  {"charge": "$.id"},
        "from_arguments": {"amount": "$.amount"},
        "static": {"reason": "saga rollback"},
        "server": null
      },
      "policy_args": {"amount": "$.amount"},
      "description": "…"
    },
    "search_docs": {"semantics": "REVERSIBLE"}
  }
}
```

| Field | Meaning |
|---|---|
| `mode` | `enforce` or `observe` |
| `unknown_semantics` | what an undeclared tool is treated as. `null` = refuse. Setting `REVERSIBLE` is how a deployment says "my unclassified tools are all reads" — a claim it should make explicitly, in a file someone signed |
| `semantics` | `REVERSIBLE` / `COMPENSABLE` / `IRREVERSIBLE` |
| `compensate.tool` | the call that undoes this one |
| `compensate.args` | extracted from the forward **result** (`$.a.b[0]` paths) |
| `compensate.from_arguments` | extracted from the forward **arguments** |
| `compensate.static` | literal values |
| `policy_args` | arguments the gate evaluates, extracted by path — an MCP tool is free to nest `amount` anywhere in its schema |

The path language is deliberately tiny (`$.a.b[0]`). A policy file is a security
artifact, and a full expression language in one would be a place to hide
behaviour.

**Validation refuses two contradictions outright:**
- `COMPENSABLE` with no `compensate` — that would report a clean rollback while
  the charge stands.
- `IRREVERSIBLE` *with* a `compensate` — one of the two is wrong, and guessing
  which would be a guess about whether a real effect can be undone.

The inverse still has to be declared — it just moves from code to a file. **That
is the enterprise feature, not a compromise**: the person who should decide
whether `create_charge` needs a human is not the person who wrote the agent, and
a file is something a security team can review, diff, and sign off.

### The boundary problem, stated plainly

MCP is request/response and has no notion of a transaction. Nothing in the
protocol says "this run failed, undo it," and a single `tools/call` can't roll
itself back. So `--boundary` picks where the boundary comes from:

| `--boundary` | The transaction is | Notes |
|---|---|---|
| `session` **(default)** | the connection | clean disconnect commits; a dropped connection is a crash the daemon already handles |
| `explicit` | whatever the model declares | injects `saga_commit` / `saga_rollback` / `saga_status` into the tool list. A still-open saga at disconnect is **rolled back**, because the caller opted into declaring boundaries and did not declare this one |
| `none` | nothing | gate, limits and audit still apply; no rollback. The honest setting for read-heavy servers |

**An agent that never signals failure gets a durable, gated, audited log and no
rollback.** That is a real limit of proxying, not something to paper over: the
proxy cannot infer that a model regretted something.

Reads (`REVERSIBLE` with no compensation) are gated and audited but never pushed
onto the rollback stack — putting them there would make every rollback report
list searches as `UNRESOLVED`, which trains an operator to ignore the report.

### Recovery through the proxy

The recovery daemon runs in a different process and holds no MCP connections, so
a compensation recovered from the WAL has nowhere to go until you install a
dispatcher:

```python
from agent_saga.mcp import set_mcp_dispatcher
set_mcp_dispatcher(lambda server, tool, args: my_client.call(server, tool, args))
```

Without it the daemon escalates rather than guesses — the same stance as an
unregistered connector handler.

### Zero dependencies

The proxy speaks JSON-RPC directly rather than through an MCP SDK, so it works
against any server regardless of which SDK that server was built with. Only
`tools/list` and `tools/call` are interpreted; everything else is forwarded
verbatim, so protocol features added later keep working.

---

# Part IV — Operating it

## 18. Time-travel debugger

`agent_saga/ui/`

A zero-dependency visual debugger reads any WAL and reconstructs each run.

```bash
agent-saga ui --wal-path ./agent-saga.wal --port 8080
python -m agent_saga.ui --wal-path ./agent-saga.wal      # equivalent
```

Dark enterprise UI, no build step, no `node_modules`, stdlib HTTP server:

- a sidebar of runs filterable by status;
- a LIFO timeline colour-coded by outcome (committed / compensated / orphaned /
  failed);
- an inspector showing each step's semantics, forward kwargs, and the exact
  compensation that ran — with **credentials shown as references, never values**.

It re-reads the file on each request, so a live, still-growing log is reflected
without a restart.

### Security

Binds to `127.0.0.1` by default. A WAL can contain business data (customer ids,
amounts, object ids), so exposing it must be deliberate.

```bash
agent-saga ui --host 0.0.0.0 --auth          # mints a token and prints the URL
agent-saga ui --token "$MY_TOKEN"            # or AGENT_SAGA_UI_TOKEN
```

The CLI warns when you bind beyond localhost, and warns again if you do it
without a token. Tokens are compared constant-time, accepted via
`Authorization: Bearer <t>` or `?token=` (so a browser can open one URL).

### HTTP API

| Route | Returns |
|---|---|
| `GET /` | the dashboard |
| `GET /api/meta` | WAL path, existence, size |
| `GET /api/sagas` | summaries, newest first, plus `corrupt_lines` |
| `GET /api/sagas/<saga_id>` | full step-by-step detail |

Programmatic use:

```python
from agent_saga.ui.reader import SagaWALReader
reader = SagaWALReader("./agent-saga.wal")
reader.list_sagas()
reader.get_saga(saga_id)
```

## 19. Observability

### Correlation ids in logs

`agent_saga/observability/__init__.py`

```python
from agent_saga import configure_logging
configure_logging(level="INFO", json=False)   # json=True for a log pipeline
```

Every log record emitted under a saga carries `saga_id` and `step_id`, attached
by a `logging.Filter` reading `contextvars` — so concurrent sagas never bleed ids
into each other's lines, and an operator can `grep` one id across a whole
rollback (the forward call, the failure, and every compensation).

`configure_logging()` is **opt-in and idempotent**: it replaces only the handler
it previously installed, leaves other handlers alone, and never touches the root
logger. Importing the library reconfigures nothing.

`current_correlation()` returns `(saga_id, step_id)` for your own logging.

### OpenTelemetry

`agent_saga/observability/otel.py` · `pip install agent-saga[opentelemetry]`

```python
from agent_saga import setup_telemetry
setup_telemetry()          # opt-in; a NoOpTracer is the default
```

| Span | Attributes |
|---|---|
| `saga.execute` (root) | `saga.id`, `saga.status` = `COMPLETED` / `ROLLED_BACK` / `FAILED` |
| `saga.step.<tool>` | `saga.step_id`, `saga.tool`, `saga.semantics`, `saga.is_compensation=false` |
| `saga.rollback.<tool>` | same, `saga.is_compensation=true` |

Exceptions are recorded with ERROR status. `trace_id` / `span_id` are stamped
onto every log record, so logs and traces join in either direction.

`ROLLED_BACK` and `FAILED` are distinct on purpose: one means we cleaned up, the
other means we could not.

**Zero-dependency contract:** when `opentelemetry` is absent, `get_tracer()`
returns a `NoOpTracer` whose spans are real context managers that do nothing —
so every instrumentation site has exactly one code path, with no `if tracer:`
guards scattered through the engine. Importing `agent_saga` never touches
`opentelemetry`.

## 20. Thread pools and tuning

`agent_saga/executors.py`

Sync work falls into two classes with completely different availability
requirements, and `asyncio.to_thread` merges them:

- **Tool work** — a blocking connector call. Arbitrary duration, arbitrary count,
  entirely outside our control.
- **WAL flushing** — one short fsync, on the critical path of every durable step
  in the process.

On the default executor, a burst of slow tool calls saturates the pool, the
flusher cannot get a thread, and **every `barrier()` in the process blocks** — so
ten slow Salesforce calls stall every saga, including ones touching nothing but
Postgres. That is head-of-line blocking on a shared resource: an availability
bug, not a tuning knob.

So the two are separated:

- Each WAL owns a **private single-thread executor** (it is a single writer; one
  thread is exactly right), which nothing else can ever occupy.
- Tool work runs on a **bounded, instrumented, resizable pool**, sized for I/O
  concurrency: `max(32, min(256, cpu_count * 16))` by default.

```python
from agent_saga import configure_tool_executor, tool_executor_stats

configure_tool_executor(max_workers=128)   # or set AGENT_SAGA_TOOL_WORKERS
tool_executor_stats()
# {"max_workers": …, "in_flight": …, "saturated": …, "max_queue_wait_ms": …}
```

Point a metrics scrape at `tool_executor_stats()`. A non-zero `saturated` with a
rising `max_queue_wait_ms` means compensations are queueing — widen the pool.

Resizing shuts the old pool down **without waiting**, so in-flight compensations
finish on their own threads. Abandoning a half-run compensation would be far
worse than briefly holding two pools.

Both pools propagate `contextvars` into the worker thread, so correlation ids
survive the hop. Async-native compensations (Salesforce, Postgres via `asyncpg`)
skip the thread hop entirely.

### Performance profile

Two profiles, never blended:

| Path | p50 | p95 | Notes |
|---|---|---|---|
| `REVERSIBLE` (fast) | ~10 µs | ~15 µs | lock-free append, no fsync |
| `COMPENSABLE` (durable) | ~3–6 ms | — | two fsync barriers; **hardware-specific** |

> Durable-path latency is a property of your disk, not this library. The numbers
> above are Windows/NTFS on a dev machine and **must be re-measured on your
> deployment target** before you quote them. Run `bench/bench_core.py` and
> `bench/bench_wal.py`; CI re-runs them on Linux and reports median-of-p99.

## 21. CLI reference

One console script is installed: **`agent-saga`**. Stdlib `argparse`, no
click/typer — the tool runs with nothing beyond the standard library.

```
agent-saga ui       [--wal-path PATH] [--port 8080] [--host 127.0.0.1]
                    [--token TOKEN] [--auth]
agent-saga verify   [--wal-path PATH] [--strict]
agent-saga export   [--wal-path PATH] --out DIR [--allow-broken]
agent-saga mcp      [--policy FILE] [--observe] [--emit-policy FILE]
                    [--boundary session|explicit|none] [--wal-path PATH]
                    -- <upstream server command>
```

Default `--wal-path` everywhere is `./agent-saga.wal`.

**Exit codes**

| Command | 0 | 1 | 2 |
|---|---|---|---|
| `verify` | chain intact | chain broken | WAL unreadable |
| `export` | bundle written, intact | written but broken, or refused | WAL unreadable |
| `mcp` | proxy exited cleanly | — | bad policy / no server command |

`verify` is designed to be a CI gate and a cron job.

There is **no `recoveryd` console script** — run the daemon as a Python program
(§12).

## 22. Configuration reference

### Environment variables

| Variable | Default | Effect |
|---|---|---|
| `AGENT_SAGA_WAL_KEY` | unset | Fernet key for WAL-at-rest encryption. Unset → plaintext |
| `AGENT_SAGA_UI_TOKEN` | unset | Bearer token required by the debugger UI |
| `AGENT_SAGA_SNAPSHOT_DIR` | `.agent_saga_snapshots` | Root for `FileSnapshotStore` |
| `AGENT_SAGA_CRED_<REF>` | unset | Fallback credential resolution when no resolver is set |
| `AGENT_SAGA_TOOL_WORKERS` | `max(32, min(256, cpu*16))` | Tool pool size |
| `AGENT_SAGA_PG_DRIVER` | auto | Pin the Postgres driver so a transitive `asyncpg` install cannot silently change it |

### Injection points

Every global is swappable, and each has a matched getter:

```python
set_limit_store(store)             # limits.py   — REQUIRED for multi-process
set_semantic_locks(manager)        # locks.py    — REQUIRED for multi-node
set_snapshot_store(store)          # durable.py  — must be shared with the daemon
set_wal_encryptor(encryptor)       # encryption.py
set_credential_resolver(fn)        # connectors/_secrets.py
set_tool_executor(executor)        # executors.py
configure_tool_executor(max_workers=N)
set_mcp_dispatcher(fn)             # mcp/proxy.py — REQUIRED for MCP recovery
setup_telemetry(tracer_provider)   # observability/otel.py
configure_logging(...)             # observability/__init__.py
```

Plus per-instance injection: `RecoveryDaemon(lock=..., ledger=...)`,
`SagaContext(gate=..., wal=...)`, `FileWAL(encryptor=..., backpressure=...)`.

## 23. Deployment checklists

### Single process, single host

The defaults are correct. Nothing to configure.

- `FileWAL` on a durable local volume
- `InProcessLimitStore`, `SemanticLockManager`, `FileLock`, `FileLedger`
- One `RecoveryDaemon` running `watch()` beside the agent
- `agent-saga verify` on a cron job

### Multi-process on one host

- [ ] `set_limit_store(RedisLimitStore(...))` — **a local budget fails open**;
      N processes each grant the full allowance
- [ ] `set_semantic_locks(RedisSemanticLocks(...))` if two processes can touch
      the same business resource
- [ ] Each process gets its **own** WAL file (the chain is per-writer by
      construction)
- [ ] A daemon per WAL file, or one daemon iterating over them

### Fleet (multi-node)

- [ ] `RedisWAL` — and read the durability caveat in §10 before putting money
      through it (`appendfsync always` + `wait_replicas>=1`)
- [ ] `RedisLedger` — **mandatory** with a shared WAL, or two daemons
      double-compensate
- [ ] `RedisLimitStore`
- [ ] `RedisSemanticLocks`
- [ ] A **distributed** `RecoveryLock` — a filesystem lock over NFS is not
      trustworthy. None ships in-tree; inject one
- [ ] A shared `SnapshotStore` (S3/GCS-backed) reachable by agents and daemons
- [ ] Daemons import **exactly the same handler modules** as the agents

### Before production, regardless

- [ ] Run the daemon `dry_run=True` for a week and read what it says it would do
- [ ] Re-measure the durable-path latency on your actual disk
- [ ] Set `set_credential_resolver()` to your real secret store
- [ ] Decide `AGENT_SAGA_WAL_KEY` — encrypted or deliberately not
- [ ] Wire `tool_executor_stats()` into metrics
- [ ] Schedule `compact()` (daemon) and `SnapshotGC.collect()`
- [ ] Schedule `agent-saga verify` and `agent-saga export` to WORM storage
- [ ] Never bind the UI beyond `127.0.0.1` without `--auth`

## 24. Troubleshooting

**`PreFlightViolation: [BLOCK] … No approval provider is configured`**
The default rule sends `IRREVERSIBLE` to `REQUIRE_APPROVAL`, and with no
provider that becomes a BLOCK. Either pass `approval_provider=` to the gate, or
reclassify the action if it is genuinely compensable.

**A limit blocks with `limit … applies to X but its argument 'amount' is
missing`**
The value is hidden in a closure. Declare it: `policy_args={"amount": amount}`.

**`WALBackpressure: WAL buffer full`**
The flush loop is not draining fast enough. Check disk health first; then either
raise `max_buffer` or switch to `BackpressurePolicy.BLOCK`. Do **not** reach for
`DROP_SILENT` on a path that moves money.

**`WALStalled`**
The sink is not acknowledging writes — full volume, wedged device, unreachable
Redis. This is the engine refusing to report an intent as durable when it isn't.

**Every dangling saga escalates with `handler not registered`**
The daemon has not imported the module that calls `@compensator`. This is by
design. Import the same connector/tool modules in the daemon process.

**`… contains N encrypted record(s) but no WAL key is configured`**
The daemon needs the writer's key. Set `AGENT_SAGA_WAL_KEY` or call
`set_wal_encryptor()`. It refuses to read the log as empty, because that would
silently abandon every crashed saga in it.

**Warning: `has an in-process-only compensation`**
The compensation has no `handler`, or its kwargs don't survive a JSON round
trip. It rolls back fine in-process, but a crash makes the effect unrecoverable.
Add a `@compensator`-registered handler and JSON-safe kwargs.

**Warning: `RecoveryDaemon has a shared WAL backend but a node-local ledger`**
Two daemons cannot see each other's successes and may compensate the same step
twice. Pass `RedisLedger`.

**`SemanticLockConflictError`**
Another saga is mid-transaction on that resource. This is the lock working —
proceeding would risk a dirty read or lost update. Retry later, or pass a
positive `timeout` to wait.

**`ConcurrentModification` / `StaleObject` / `StaleFile` during rollback**
Something outside the saga changed the row/object/file after the saga wrote it.
Restoring would discard their change, so the connector refuses and escalates.
Resolve by hand.

**`RuntimeError: RedisSemanticLocks is distributed and cannot be acquired
synchronously`**
Use `await ctx.acquire_semantic_lock(resource_id)` before registering, and pass
`lock=False` to `tentative()`.

**Rollback report shows `ORPHANED`**
The step executed and no compensation existed — either none was supplied, or the
factory returned `None` (e.g. an `UNKNOWN` Stripe charge with no id). This is
loud on purpose. Reconcile manually.

**Rollback report shows `UNRESOLVED`**
A compensation failed and `halt_on_compensation_failure=True` stopped the
unwind. These steps were never attempted. Fix the failing compensation, then
re-drive.

## 25. Module map

```
agent_saga/
├── __init__.py            the public API surface (everything re-exported)
├── semantics.py           ActionSemantics, Compensation, SagaStep, StepState
├── context.py             SagaContext — the 6-phase execute, LIFO rollback,
│                          leases, tentative/lock lifecycle, RollbackReport
├── decorator.py           saga_scope, @saga, @tool, current_saga
├── gate.py                PreFlightGate, Rule, Verdict, predicates
├── limits.py              BudgetLimit, RateLimit, scopes, limit stores
├── registry.py            @compensator — named cross-process handlers
├── idempotency.py         deterministic keys + the execution ledger reader
├── recovery.py            RecoveryDaemon, parse_wal, DanglingSaga, Resolution
├── ledger.py              FileLedger / InMemoryLedger / RedisLedger
├── locks.py               FileLock, InProcessLock; SemanticLockManager,
│                          RedisSemanticLocks
├── integrity.py           hash chain, verify, redact, attestations, WORM export
├── encryption.py          BYOK WAL-at-rest (Fernet), encode/decode_line
├── executors.py           BoundedExecutor, pool isolation + stats
├── snapshot.py            reversible() — in-process REVERSIBLE snapshots
├── durable.py             snapshot_file() — durable COMPENSABLE snapshots
├── gc.py                  SnapshotGC — conservative snapshot-store sweep
├── cli.py                 the `agent-saga` command
├── wal/
│   ├── base.py            BaseWAL contract, BufferedWAL machinery, backpressure
│   ├── file_wal.py        FileWAL (== AsyncWAL), the zero-dep default
│   └── redis_wal.py       RedisWAL, gseq, read_since, WAIT-based barrier
├── mcp/
│   ├── policy.py          ProxyPolicy, ToolPolicy, CompensationSpec, $-paths
│   ├── proxy.py           SagaMCPProxy — interception, boundaries, dispatcher
│   └── stdio.py           JSON-RPC stdio transport + UpstreamServer
├── connectors/
│   ├── _secrets.py        credential references, assert_no_secrets
│   ├── stripe.py          charge / refund
│   ├── postgres.py        update / insert / delete + guarded restores
│   └── salesforce.py      patch_object / revert_object
├── adapters/
│   ├── _common.py         build_runner — the shared routing core
│   ├── langgraph.py  crewai.py  openai_agents.py  llamaindex.py  autogen.py
├── observability/
│   ├── __init__.py        correlation ids, Text/Json formatters
│   └── otel.py            SagaTracer, NoOpTracer, setup_telemetry
├── patterns/
│   └── tentative.py       TentativeResource, tentative()
└── ui/
    ├── reader.py          SagaWALReader, iter_records, scrub
    ├── server.py          stdlib HTTP server + bearer auth
    └── templates/         dashboard.html

examples/    demo.py · chaos_demo.py · multi_domain.py
bench/       bench_core.py · bench_wal.py · summarize.py
tests/       406 tests
```

## 26. Known gaps

Tracked openly; see [SECURITY.md](SECURITY.md).

- **No shipped distributed recovery lock.** The interface exists; no backend
  ships in-tree for the recovery claim (`RedisSemanticLocks` covers *semantic*
  locks, which is a different concern). Inject one for a multi-host fleet.
- **Async-native connectors are partial.** Salesforce and Postgres-via-`asyncpg`
  are async-native; Stripe and Postgres-via-`psycopg` go through the thread pool.
- **KMS/Vault key resolution is deliberately absent** from this BYOK core — it is
  an intended Enterprise-tier feature. `set_credential_resolver()` is the hook.
- **The MCP proxy cannot infer regret.** With `--boundary session` or `none`, an
  agent that never signals failure gets a durable, gated, audited log and no
  rollback.
- **The hash chain is per-writer.** Correlating logs across a fleet is a
  control-plane concern.
- **Not yet published to PyPI.** Install from source.
- **Status: pre-alpha.**

---

## License

Apache-2.0. See [LICENSE](LICENSE), [NOTICE](NOTICE), and
[TRADEMARKS.md](TRADEMARKS.md). Copyright 2026 SagaOps.

Permissive on purpose: this library is `import`ed directly into the process that
moves your money, and a copyleft dependency on that path is something most legal
teams will not clear. Apache-2.0 also carries an **explicit patent grant**
(section 3), which is the clause enterprise review actually looks for. The code
is open; the **name** is not (section 6 grants no trademark rights) — fork it,
just rename it.
