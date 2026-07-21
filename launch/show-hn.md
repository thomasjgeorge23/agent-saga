# Show HN launch kit — SagaOps / agent-saga

Everything here is written to be *defensible*. HN will fact-check the post in
the comments; don't put a number in it we can't reproduce on a Linux box in
front of them. Placeholders in ⟨angle brackets⟩ must be filled before posting.

---

## Title

Keep it plain. No superlatives, no "revolutionary." Options, best first:

1. **Show HN: SagaOps – Transactional rollback for autonomous AI agents**
2. Show HN: An undo button for AI agents that touch real systems
3. Show HN: Compensating-transaction engine for LLM agents (open source, Python)

Post around 8–10am ET on a weekday. Link to the GitHub repo, not the landing
page — HN prefers the source.

---

## The post (text body)

Hi HN. I've been building `agent-saga`, an open-source Python library that gives
AI agents a transactional "undo."

The problem: once you let an agent call real tools — update a Postgres row,
charge a card, patch a Salesforce record — a single hallucination or a mid-run
error leaves the world half-mutated. There's no `ROLLBACK`. The agent charged
the customer, updated three systems, and *then* picked the wrong next step.

`agent-saga` wraps tool calls in a saga boundary. Each call records a
runtime-derived compensating action; if anything in the boundary raises, the
compensations run last-in-first-out across every system the agent touched. A
`saga_scope` looks like this:

    async with saga_scope() as saga:
        await salesforce.patch_object(saga, ..., patch={"Status": "Won"})
        await stripe.charge(saga, customer_id=..., amount=42000)
        # agent raises here → charge refunded, Lead reverted, in reverse order

The part I found genuinely hard, and the reason I don't think this is "just the
saga pattern in a decorator":

1. **Most side effects aren't invertible, and pretending otherwise is the trap.**
   So "undo" is a typed decision, not one operation: REVERSIBLE (restore an
   exact snapshot), COMPENSABLE (emit an inverse that leaves a trace — a refund
   is a new ledger entry, not a rewind), or IRREVERSIBLE (an email, a wire — you
   can't). The interesting product isn't the rollback; it's a **pre-flight gate**
   that refuses to *enter* an uncompensable step without a human, before the
   effect happens.

2. **The agent chose the action, so the inverse is only knowable at runtime.**
   Durable-workflow engines make you declare the compensating step at authoring
   time. But you can't refund a charge until the charge returns its id. The
   compensation is derived from the forward call's actual result.

3. **A crash mid-saga can't orphan a real effect.** Intent is written to a WAL
   *before* the side effect. A separate recovery daemon reads the WAL after a
   crash, runs the compensations by name (idempotency-keyed, so no double
   refund), and fails closed to a human queue for anything it can't resolve.

It's Apache-free — licensed AGPL-3.0 with a commercial option. The core has zero
dependencies (`pip install agent-saga`); connectors and framework adapters
(LangGraph, CrewAI, OpenAI Agents SDK) are opt-in extras. There's also a
zero-dependency time-travel debugger that renders the WAL.

Repo: https://github.com/thomasjgeorge23/agent-saga

It's pre-alpha and I'd genuinely like the scrutiny — especially on the recovery
and idempotency logic, and on where the compensation model breaks down. Happy to
go deep in the comments.

---

## First comment (post immediately, technical depth HN rewards)

A few things I left out of the post to keep it short:

**On honesty about latency.** The hot path is in-process bookkeeping; snapshots
and fsync happen off the critical path, and REVERSIBLE steps skip the durability
barrier entirely while money-path steps pay for it (amortized by group commit
under concurrency). I'm deliberately *not* quoting a headline latency number
until the benchmark runs on a clean Linux/NVMe box in CI — I had dev-machine
numbers with a wide fsync tail and didn't trust them enough to publish. I'll post
the CI numbers here when they land rather than a figure you can't reproduce.

**On "UNKNOWN" outcomes.** A charge that times out may or may not have landed.
The library treats a failed forward call as UNKNOWN, not "didn't happen," and
still attempts an idempotent compensation — because the alternative is leaving
money on the floor.

**On what it can't do.** If a step is genuinely irreversible and it executed, the
rollback reports it as ORPHANED rather than pretending. The whole design is built
to make "we could not undo this" a loud, structured output, not a silent gap.

---

## Objection handling (paste-ready replies)

### "Why not just use database transactions?"

Because a DB transaction can't do the two things that actually matter here.
(1) It can't span systems — the agent touched Stripe, Salesforce, and Postgres in
one workflow; there's no `BEGIN` across those. (2) You can't hold `BEGIN…COMMIT`
open for the seconds (or minutes) an LLM takes to decide the next step without
pinning a connection and holding row locks the whole time — that's how you
exhaust a pool and stall unrelated traffic. Sagas exist precisely for
long-running, cross-service work where a single ACID transaction isn't available.
Where a real transaction *is* available and short, use it — this is for
everything around it.

### "Isn't this just the saga pattern / just Temporal?"

The saga pattern is the foundation, yes. The difference is who writes the
compensation. Temporal (which is excellent) has you declare the workflow graph
and its compensating steps at authoring time. With an LLM agent, a developer
didn't choose the sequence — the model did, at runtime — and the inverse depends
on the forward call's result (the charge id, the row ids). So the compensation is
derived at runtime, and the pre-flight gate that blocks uncompensable actions is
the layer that's specific to non-deterministic callers. If you're already on
Temporal and your agent's action space is fixed and hand-declared, you may not
need this.

### "How is this different from LangSmith / Datadog / observability?"

Those record what happened (tokens, traces, logs). They don't hold compensating
actions or execute them. This is on the write path, not the read path: it can
actually undo the charge, not just show you that it happened.

### "Irreversible actions exist — so isn't the whole premise broken?"

That's exactly why the typed semantics and the pre-flight gate exist. The library
doesn't claim to reverse an email or a wire. It classifies those as IRREVERSIBLE
and refuses to *start* them inside a saga without explicit human approval — and
if one somehow executed, it's reported as ORPHANED, loudly. The value for a
regulated buyer is the refusal up front, not a magic rewind.

### "Why AGPL? That kills adoption at companies that ban it."

Fair, and it's a real tradeoff. The core is AGPL so a cloud can't take it and
resell a managed version without contributing back; there's a commercial license
for teams that can't take an AGPL dependency. If AGPL is a blocker for you
specifically, open an issue on the repo — I'd rather hear the use case than lose it silently.

### "What happens if the recovery daemon itself is wrong / double-compensates?"

Compensation is idempotency-keyed and the recovery tokens are deterministic
(same saga + step → same token), with a journal, so a retrying or racing daemon
can't run the same compensation twice. Anything the daemon can't resolve with
certainty — an irreversible step, a compensation that raised, a snapshot it can't
read — halts and routes to a human rather than guessing. I'd love eyes on this
specifically; it's the scariest code in the repo.

### "Does this add latency to every tool call?"

In-process bookkeeping, yes; a network round trip, no — snapshotting and fsync
are off the hot path. REVERSIBLE steps don't touch the disk barrier at all. Real
numbers from CI to follow (see above — not going to quote a figure I can't
reproduce in front of you).

---

## Pre-launch checklist

- [x] GitHub URL filled; repo public at github.com/thomasjgeorge23/agent-saga.
- [ ] Add a commercial-license contact (email or a pinned GitHub issue).
- [ ] README front-and-center: the `saga_scope` example, install, and the
      typed-semantics explanation in the first screen.
- [ ] The Linux CI benchmark has run and the median-of-p99 numbers are ready to
      paste *if asked* (don't lead with them).
- [ ] A 30–60s screen recording of the time-travel debugger unwinding a saga,
      linked in the first comment.
- [ ] Be at a keyboard for the first 2–3 hours to answer every comment.
