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

📖 **[MANUAL.md](MANUAL.md)** — the complete reference: every subsystem, how it
works and why, CLI and configuration, deployment checklists, and troubleshooting.

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

## Crash testing

Most of this suite runs in one cooperative process, which proves the design is
coherent and proves nothing about durability. The chaos suite kills the process.

```bash
python -m pytest tests/test_chaos.py -v
```

A worker performs real, durable effects against a file-backed ledger and is then
killed with `os._exit` — no `atexit`, no `finally`, no event-loop shutdown.
Whatever holds afterwards holds because the design is right, not because
anything got to clean up. Four crash points, each leaving the log in a
structurally different state:

| Crash point | State left behind |
|---|---|
| `after_intent` | intent fsynced, **the charge never happened** |
| `after_effect` | charge happened, **its inverse is not yet durable** |
| `after_commit` | compensation descriptor durable |
| `mid_compensation` | died half way through unwinding a 3-step saga |

What the suite asserts, against the ledger rather than against the log:

- A crash after the effect is compensated, **or escalated to a human** — never a
  clean report with money still outstanding.
- Running the daemon **four times issues one refund**.
- **Two daemons racing issue one refund.**
- An interrupted rollback is *finished*, not restarted — every charge refunded
  exactly once.
- The hash chain verifies after a kill at all four points, and a torn final line
  neither hides earlier records nor stops recovery.

> **The ledger records refund *attempts* separately from refunds *applied*.** A
> real payment processor absorbs a duplicate refund via its own idempotency key,
> so a test that only checked the final balance would pass whether the guarantee
> lives in `agent-saga` or in Stripe. Recording both lets these tests assert the
> strong claim — that the second call was never *made*.

**One guarantee a crash genuinely breaks, stated rather than buried:** spend
windows live in the limit store, not the WAL. With the in-process default, a
crashed and restarted agent starts its window fresh, so a crash-loop can spend
the daily budget repeatedly. `RedisLimitStore` isn't only the multi-node answer —
it's the crash-durable one. There is a test that asserts this, so it can't
quietly stop being true.

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

### FastAPI integration

For FastAPI applications, you can use the native lifespan plugin to automatically initialize the engine, configure the default process-wide WAL, and manage the background recovery daemon.

```python
from fastapi import FastAPI
from agent_saga import saga_lifespan, saga

app = FastAPI(lifespan=saga_lifespan("path/to/wal.jsonl"))

@app.post("/checkout")
async def checkout():
    # Sagas inside routes will automatically use the default WAL
    @saga
    async def process_payment():
        ...
    return await process_payment()
```

During shutdown, the lifespan manager gracefully cancels the recovery daemon, awaits any pending compensations for active sagas, releases held semantic locks, and flushes the write-ahead log.

## Reconciliation

Every other guarantee here ends at an API response. The refund returned 200, so
the WAL says `COMPENSATED`, so the rollback report says clean. A bank does not
accept that chain, and it's right not to — a 200 is an acknowledgement, not a
fact about the ledger. It can come from an idempotency key that matched a
*different* operation, a write that was later voided, a call that reached the
wrong tenant, or a queue that accepted the work and dropped it.

So this pass ignores what the log says and asks the external system what's true.

```python
from agent_saga import reconciler, Observation

@reconciler("stripe.refund")            # same name as @compensator("stripe.refund")
async def observe_refund(*, charge_id, credential_ref=None, **kw):
    charge = await stripe.Charge.retrieve(charge_id)
    return Observation(reversed_=charge.refunded, exists=True,
                       detail=charge.status, amount=charge.amount)
```

```bash
agent-saga reconcile --wal-path ./agent-saga.wal --import myapp.reconcilers
# exit 0 clean · 1 drift · 3 nothing could be verified
```

**It also resolves `UNKNOWN`** — the hardest state in the engine. A timed-out
`POST` to Stripe may well have charged the card, and no amount of in-process
reasoning can settle it; asking the card network is the only way. That's the
case worth the whole module:

> `[DRIFT] stripe.charge: timed-out step DID land and is still standing — it was
> never compensated (amount=4200)`

**Unverifiable is never counted as confirmed.** If no `@reconciler` is
registered, or the system couldn't tell us, or the check timed out, the effect is
reported as unverifiable and the run is *not* clean. A reconciliation report that
quietly folds "couldn't check" into "fine" is worse than no report — it's the one
that gets shown to an auditor.

`Observation` is deliberately tri-state (`True` / `False` / `None`) rather than
boolean, so "I don't know" survives instead of being forced into a claim.

Run it as a separate, later pass — not inline. Payment and CRM APIs are
eventually consistent, so reading back immediately after a write reports drift
that's merely latency, and a control that cries wolf gets muted.

---

## Kill switch and quarantine

The first question in an incident is *"how do I make it stop"*, and nothing else
here answers it. Limits cap a rate; the gate refuses a category. Neither helps at
03:00 when an agent is doing something nobody predicted.

```bash
agent-saga halt --scope tool:wire.send --reason "fraud pattern" --by soc@corp
agent-saga halt --drain --reason "deploying" --by ci@corp --ttl 600
agent-saga quarantine saga-8f3c --reason "suspected duplicate charges" --by soc@corp
agent-saga status
agent-saga resume --scope tool:wire.send --by soc@corp
```

Four levers, because "stop" isn't one thing:

- **HALT** — refuse new side effects immediately, globally or scoped to
  `tool:wire.send`, `tool:stripe.*`, or `tag:eu`. An operator who can *only* stop
  everything will hesitate to stop anything.
- **DRAIN** — start no new sagas, let running ones finish. Blocking their
  remaining steps would strand every one half-done, which is the opposite of
  draining.
- **QUARANTINE** — freeze one saga. Explicitly **not** a rollback: during an
  incident, automatically reversing a hundred sagas can be far worse than leaving
  them still. The saga stops, *the recovery daemon skips it*, and a human decides.
- **TTL** — a halt nobody remembers to lift is its own outage.

Checked before limits and approvals, so a halted system doesn't spend budget
deciding to refuse or wake a human to approve a call it will reject anyway. Who
halted it, why, and when all land in the hash-chained WAL.

> **The one place this library deliberately does not fail closed.** Everywhere
> else, an unreachable backend refuses. Applied here that would make the kill
> switch the largest availability risk you own — a Redis blip halting every agent
> everywhere, the control installed to contain an incident causing one. Failing
> open instead lets anyone who can take the store down bypass the switch. So
> neither: the last known state is cached and honoured for a bounded `grace`
> window. A blip is survived; an outage is not a bypass, because once grace
> expires it fails closed. Set `grace=0` for maximum safety and accept that a
> store outage becomes a fleet outage — that tradeoff is yours to own, which is
> why it's a constructor argument and not a hidden default.

`FileSwitchStore` is the zero-setup default and warns loudly at install time that
it only stops *this process*. A kill switch that stops one pod is not a kill
switch — use `RedisSwitchStore` for a fleet, and name it in your runbook.

---

## Human approvals

The gate can refuse. Refusing is often the wrong answer — what a bank actually
wants is *a named human on the hook*, which means a real approval lifecycle, not
a callback returning a bool.

```python
from agent_saga import (PreFlightGate, ApprovalGateway, ApprovalPolicy,
                        EscalationLevel, FileApprovalStore, WebhookNotifier)

gate = PreFlightGate(approval_provider=ApprovalGateway(
    store=FileApprovalStore(),                       # RedisApprovalStore for a fleet
    notifier=WebhookNotifier(os.environ["SLACK_WEBHOOK"]),
    wal=wal,
    policy=ApprovalPolicy(timeout=900, levels=(
        EscalationLevel(targets=("@oncall",)),
        EscalationLevel(targets=("@head-of-risk",), after_seconds=300))),
))
```

```bash
agent-saga approvals list
  [PENDING] wire.send -- Action cannot be undone (rule irreversible, 42s old, id af83c011)
        amount: 80000
        to: acct_9
agent-saga approvals approve af83c011... --approver risk@corp --note "verified by phone"
```

What the callback couldn't do, and each is a way a real approval goes wrong:

- **Survive a crash.** Requests are written to a shared store *and the WAL*
  before anyone is asked, so a dead process doesn't strand an approver's "yes".
- **Be answered from elsewhere.** The human clicks in Slack, which reaches some
  web process — not the agent. The decision lands in the store; the waiting saga
  observes it there. No inbound connectivity to the agent, because agents run in
  places that have none.
- **Time out.** Mandatory deadline, and **expiry denies**. An unanswered prompt
  would otherwise hold the saga's lease, semantic locks and tentative resources
  open indefinitely.
- **Escalate.** One person is asleep; the chain asks the next and records that
  it did.
- **Not ask twice.** The request id is derived from `(saga, step, tool, rule)`,
  so a retried step finds its existing decision instead of re-prompting a human
  whose second answer would authorize a second effect.
- **Break-glass.** Emergency override grants — and writes a distinct
  `APPROVAL_BREAK_GLASS` record flagged `requires_review`. A break-glass that
  looks like a normal approval in the log defeats the point of having one.

Every path fails closed: timeout denies, unreachable store denies, broken Slack
webhook denies. A failed integration must never authorize spending — and unlike
a limiter, what an approval control lets through is precisely the action a human
was meant to see.

The approver, the note, the amount and the timestamp all land in the
hash-chained WAL, so *"prove no agent moved money without a named human"* is
answerable by reading the log — and rewriting who approved it breaks the chain.

The CLI refuses an approval with no `--approver`: an anonymous approval is an
audit trail that proves nothing, which is the only thing the record is for.

---

## MCP proxy — no change to the agent

Wrapping tools asks the agent's author to refactor the thing they're already
nervous about. An MCP client talks to servers over a socket, so a proxy can sit
in that socket and give the same guarantees to an agent that has no idea it's
there.

```bash
# 1. Learn what your agent actually calls, changing nothing.
agent-saga mcp --observe --emit-policy saga-policy.json -- python -m my_mcp_server

# 2. Classify what it found (everything arrives IRREVERSIBLE with a TODO), then:
agent-saga mcp --policy saga-policy.json -- python -m my_mcp_server
```

```json
{
  "mode": "enforce",
  "tools": {
    "stripe__create_charge": {
      "semantics": "COMPENSABLE",
      "compensate": {"tool": "stripe__create_refund", "args": {"charge": "$.id"}},
      "policy_args": {"amount": "$.amount"}
    },
    "search_docs": {"semantics": "REVERSIBLE"}
  }
}
```

The inverse still has to be declared — it just moves from code to a file. That's
the enterprise feature, not a compromise: the person who should decide whether
`create_charge` needs a human is not the person who wrote the agent, and a file
is something a security team can review, diff, and sign off.

**Undeclared tools are refused.** Not allowed-with-a-warning: if nobody has said
whether a tool can be undone, it doesn't reach a real system. Observe mode is
the ramp — it forwards everything, records the real tool surface, and emits a
skeleton. Every entry comes back `IRREVERSIBLE` with a TODO, deliberately: a
generator that guessed `COMPENSABLE` and invented an inverse would be asserting
that a real financial operation is undoable on the evidence of a tool *name*,
which is the one guess this project exists to refuse. Reviewers downgrade what's
safe; the file never upgrades itself.

**The boundary problem, stated plainly.** MCP is request/response and has no
notion of a transaction — nothing in the protocol says "this run failed, undo
it," and a single `tools/call` can't roll itself back. So `--boundary` picks
where the boundary comes from: `session` (the connection is the transaction;
clean disconnect commits, dropped connection is a crash the daemon already
handles), `explicit` (injects `saga_commit`/`saga_rollback` into the tool list
for the model or app to drive), or `none` (gate, limits and audit, no rollback).
An agent that never signals failure gets a durable, gated, audited log and no
rollback. That's a real limit of proxying — the proxy cannot infer that a model
regretted something.

Zero dependencies: it speaks JSON-RPC directly rather than through an MCP SDK,
so it works against any server regardless of which SDK that server was built
with. Only `tools/list` and `tools/call` are interpreted; everything else is
forwarded verbatim, so protocol features added later keep working.

---

## Tamper-evident audit log

A WAL is already the record of what an agent did with real money. Chained, it
becomes something an auditor can rely on: every record commits to its
predecessor, so any edit, reorder, insertion, or deletion invalidates the chain
and the verifier names the first record that stops adding up.

```bash
agent-saga verify --wal-path ./agent-saga.wal     # exit 0 only if intact
agent-saga export --wal-path ./agent-saga.wal --out ./audit-2026-07
```

On by default — one SHA-256 per record, on the flusher thread, off the caller's
hot path. A log that is only *sometimes* chained is not evidence of anything.

**The chain never hashes the payload directly.** It hashes a salted content
digest, and that indirection is what makes the two *legitimate* mutations
possible without weakening the proof:

- **GDPR erasure.** `redact_record` destroys the payload **and the salt**, then
  the chain still verifies. What survives is proof that a record existed, when,
  in what order, and of what type — with its contents irrecoverable, including
  by whoever holds the log. Dropping the salt is load-bearing: `{"amount": 4200}`
  has few plausible preimages, so an unsalted digest would leak the value it was
  meant to erase.
- **Compaction.** `compact()` legitimately drops settled sagas, which looks
  exactly like an attacker deleting the record of a charge — both are missing
  sequence numbers. So compaction writes a **chained attestation** naming
  precisely which sequences left and the digest of what they were. A gap is
  accepted only when an attestation accounts for every sequence inside it, and
  attestations themselves survive later compactions, because housekeeping that
  erased them would turn an explained gap back into an apparent attack.

`export` writes a WORM bundle — newline-delimited JSON plus a manifest carrying
the chain head, the bundle's own SHA-256, and *the verification rule in prose*,
so an auditor can re-check it years from now with `sha256sum` and nothing else.
An archive readable only by the tool that wrote it is not evidence, it's a
dependency. Export refuses a broken chain unless you pass `--allow-broken`,
which labels it — exporting a broken chain silently would launder it into an
artifact that looks authoritative.

> **Scope.** The chain proves *one writer's* log is intact, and is per-process by
> construction: a single chain across nodes would need a global lock on every
> append, which is a distributed transaction on the hot path of every tool call.
> For a fleet, each node's log is independently provable; correlating them is a
> control-plane concern.

---

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

Pre-alpha, by SagaOps. Implemented and tested (461 tests; the base suite runs
with only `pytest`; optional extras add their own SDKs):

- Core engine, recovery daemon (truncation-tolerant), and a time-travel debugger
  with optional bearer-token auth for shared environments.
- Durable human-in-the-loop approvals: requests survive a crash, are answerable
  from another process (Slack → web → store, no inbound connectivity to the
  agent), escalate through a chain, and **deny on timeout**. Break-glass
  overrides are recorded distinctly and flagged for post-hoc review.
- Per-step `RetryPolicy` (linear/exponential backoff, typed include/exclude
  lists) and `fallback_action`, retried *inside* one step so the idempotency key
  and approval id never change between attempts.
- Connectors: Stripe, Postgres (full CRUD — update/insert/delete with compound
  primary keys), Salesforce.
- Adapters: LangGraph, CrewAI, OpenAI Agents SDK, LlamaIndex, AutoGen.
- A FastAPI lifespan (`saga_lifespan`) that starts the WAL, runs the recovery
  daemon in the background, and drains in-flight sagas on shutdown.
- Snapshot capture: in-process (`REVERSIBLE`) and durable crash-recoverable
  (`COMPENSABLE`), with a conservative store GC sweep.
- Durability & safety: configurable WAL backpressure (`RAISE` by default —
  never silently drops a record), and optional BYOK WAL-at-rest encryption
  (`pip install agent-saga[encryption]`; key via `AGENT_SAGA_WAL_KEY` or an
  injected encryptor — a reader without the key fails loud, never silent).
- Recovery locking: an injectable lock interface, defaulting to a local file
  lock (no Redis in-tree — supply a distributed backend if you run a fleet).
- Pluggable WAL backends behind `BaseWAL`: `FileWAL` (the zero-dependency
  default, fsync-durable), `RedisWAL` for multi-node deployments
  (`pip install agent-saga[redis]`), and `PostgresWAL` for a shared log in the
  database you already run (`pip install agent-saga[postgres]`). `barrier()` is
  part of the interface, not an extra — a backend without a durability fence is
  fire-and-forget. Redis is documented as a *weaker* durability class than fsync
  and supports `WAIT` for replica acknowledgment; read that section before
  putting money through it. `PostgresWAL` inherits Postgres's own durability
  (`synchronous_commit`), but does not yet implement `compact()`.
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
