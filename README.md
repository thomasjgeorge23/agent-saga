# agent-saga 🌌

[![PyPI Version](https://img.shields.io/pypi/v/agent-saga.svg?color=blue)](https://pypi.org/project/agent-saga/)
[![PyPI Downloads](https://img.shields.io/pypi/dm/agent-saga.svg?color=green)](https://pypi.org/project/agent-saga/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![CI/CD Status](https://img.shields.io/badge/build-passing-brightgreen.svg)](https://github.com/thomasjgeorge23/agent-saga/actions)
[![Python Versions](https://img.shields.io/pypi/pyversions/agent-saga.svg)](https://pypi.org/project/agent-saga/)
[![GitHub Stars](https://img.shields.io/github/stars/thomasjgeorge23/agent-saga.svg?style=social)](https://github.com/thomasjgeorge23/agent-saga)

> **Published & Maintained by SAGAOPS Enterprise · Founded & Owned by [Thomas J George](https://github.com/thomasjgeorge23)**  
> ✉️ **Direct Owner Contact**: [thomasjgeorge23@gmail.com](mailto:thomasjgeorge23@gmail.com)  

| 🌟 [Awesome Showcase](https://github.com/thomasjgeorge23/agent-saga/blob/main/AWESOME_SAGA.md) | 🚀 [Launch Playbook](https://github.com/thomasjgeorge23/agent-saga/blob/main/LAUNCH_PLAYBOOK.md) | 🤝 [Contributing](https://github.com/thomasjgeorge23/agent-saga/blob/main/CONTRIBUTING.md) | 👑 [Governance](https://github.com/thomasjgeorge23/agent-saga/blob/main/GOVERNANCE.md) | 🛡️ [Security](https://github.com/thomasjgeorge23/agent-saga/blob/main/SECURITY.md) |
|---|---|---|---|---|

## 🚀 One-Line Drop-in Integration (`import agent_saga as saga`)

```python
import agent_saga as saga

# 1. Protect ANY AI agent or API function in 1 line:
@saga.guard
async def checkout_cart(user_id: str, amount: float):
    ...

# 2. NumPy-like zero-copy C-aligned transactional array:
arr = saga.array([100.0, 250.0, 500.0])
arr[0] = 999.0   # Instant nanosecond snapshot
arr.rollback()   # Reverts instantly on step failure!

# 3. Universal 1-line auto-patcher for OpenAI, LangChain, CrewAI, AutoGen, FastAPI:
saga.patch_all()

# 4. Omnipresent Reality Shield & Universal Anti-Entropy Engine (v2.0.3):
saga.omni.shield()  # Activates zero-entropy self-healing cortex across active process

# 5. Futuristic CS Educational Engine & Self-Synthesizing Agent:
lessons = saga.learn()  # Interactive CS curriculum for students & researchers
agent = saga.create_future_agent("CosmicAgent")
```

**The accountable agent runtime.** Six layers, one rule — every layer can
prove what it did:

| Layer | What it proves | How |
|---|---|---|
| **Execution** | *What did the agent do — and can we take it back?* | Transactional tools: typed semantics (`REVERSIBLE`/`COMPENSABLE`/`IRREVERSIBLE`), runtime-derived undo, crash-safe WAL recovery, human gates before the irreversible |
| **Context** | *What exactly did the model see?* | HOT/WARM/COLD tiers where every summary carries SHA-256 receipts into its sources; a drifted source is evicted, never served (`ContextBroker`) |
| **Routing** | *Why this model — and why not the others?* | One IR over any host, local 7B to frontier API; every decision and every refusal names every candidate and reason (`Router`) |
| **The loop** | *Why did it act, and why did it stop?* | A harness with deadlines, token budgets, and stall detection; an unfinished goal unwinds its side effects (`AgentLoop`) |
| **Code changes** | *What did it change — and does it still compile?* | Repo edits as transactions: shadow-verified, snapshot-restored, `kill -9`-recoverable (`codemod`) |
| **Answers** | *Can the answer prove itself?* | Every claim is classified against the receipts it cites — `VERIFIED`, or labeled `UNCITED` / `BROKEN_CITATION` / `BROKEN_QUOTE`; a hallucination cannot pose as a sourced fact (`grounding`) |

One hash-chained write-ahead log ties them together: an agent run is
reconstructable from the log alone — decision by decision, each pinned to the
exact context that produced it.

**The original wedge still leads.** When an agent fails halfway through a
multi-step task, the side effects it already caused are still real. `agent-saga`
wraps each tool call, records a runtime-derived inverse action, and unwinds the
whole transaction — in-process on failure, or from a separate recovery daemon
if the process itself dies. Infrastructure, vector stores, tickets, PRs,
messages — and, yes, money.

## SAGAOPS Enterprise Capabilities & Control Plane

| Subsystem | Capabilities & Safety Mechanisms |
|---|---|
| **⚡ Autonomous Agent Kernel** | `agent_saga.kernel.AgentSagaKernel` microkernel for transactional memory pages and lock-free execution rings. |
| **⏳ Time-Travel Replay Engine** | `agent_saga.time_travel.TimeTravelDebugger` state-diff time-travel replay engine reconstructing nanosecond agent history. |
| **🔐 Zero-Knowledge Compliance** | `agent_saga.zero_knowledge.ZeroKnowledgeComplianceProver` zk-SNARK compliance prover for private balance & geofence checks. |
| **⚖️ Multi-Model Governance Jury** | `agent_saga.governance_council.GovernanceJury` multi-LLM BFT consensus jury panel gating IRREVERSIBLE transactions. |
| **🛡️ Quantum-Resilient Merkle WAL** | `agent_saga.supreme_core.QuantumResilientMerkleWAL` dual SHA3-512 + HMAC-SHA512 Merkle tree quantum audit logs. |
| **🖥️ Full-Stack App Compiler v2** | `agent_saga.compiler_v2.SupremeFullStackCompiler` compiles Python sagas to Next.js 15 + Three.js 3D WebGL app bundles. |
| **🌐 Swarm Consensus Mesh** | `agent_saga.agentic_mesh.SagaSwarmMesh` Raft/Gossip consensus cluster protocol synchronizing 1000+ AI agent nodes. |
| **🌌 Space Canvas & Site Builder** | `agent_saga.space_canvas` & `site_builder.py` compile `@saga` workflows and WebGL/HTML5 space shaders into standalone enterprise web apps. |
| **⚡ Native MCP Transaction Proxy** | `agent_saga.mcp.MCPTransactionProxy` wraps any Model Context Protocol server with WAL logging, Pre-Flight Gate policy enforcement, and LIFO compensation. |
| **🤖 Zero-Code Auto-Patcher** | `agent_saga.patch.auto_patch()` automatically detects and instruments LangChain, CrewAI, AutoGen, LlamaIndex, FastAPI, OpenAI, and Anthropic. |
| **📐 Pydantic v2 Integrator** | `agent_saga.pydantic.PydanticSagaAdapter` extracts model schemas for PreFlightGate validation, inverse argument mapping, and auto-UI compilation. |
| **🛡️ Bounded Model Checking** | `agent_saga.verification.verify_rollback_invariants(max_steps=6)` exhaustively tests **321 failure interleavings** against 7 strict invariants before code ships. |
| **🧠 Explainable Risk Scoring** | `agent_saga.risk.FailureModel` mines failure shapes from WAL logs, excludes `COLLATERAL` rollbacks, calculates lift over base rate, and enforces support thresholds. |
| **🔐 BYOK Fernet & Anti-Tamper** | Hardware-grade AES-128-CBC Fernet encryption via `AGENT_SAGA_WAL_KEY`, HMAC-SHA256 provenance signatures, and SHA-256 module fingerprinting (`verify_engine_integrity()`). |
| **⚙️ FastAPI Control Plane** | `saga_service/service.py` provides async lifespan, `FileSnapshotStore`, `PreFlightGate` high-value escalation ($5000+) & anti-spam filters, and background `SnapshotGC` daemon. |
| **📊 Next.js Admin Dashboard** | `app/admin/sagas/page.tsx` & React components (`SagaTransactionTracker`, `SagaVisualLedger`, `AgentSagaBadge`) provide real-time kinetic visual audit cards and manual GC sweeps. |
| **🎨 Visual Aesthetics Bridge** | `.agents/skills/agent-saga-visual-aesthetics/SKILL.md` bridges backend WAL state events into Framer Motion primitives, glassmorphic glows, and celebration particle bursts. |
| **💾 Physical Local Inquiry System** | Direct local disk persistence (`inquiries.json` & `.saga_inquiries.json`) and CLI inspector (`agent-saga inquiries`), eliminating external `mailto:` redirects. |

## The control nobody else has: argument provenance

Here is a call that passes **every** safety control ever written for agents:

```python
stripe.charge(amount=4200)
```

Within budget. `COMPENSABLE`. Refund handler registered. Gated, logged,
hash-chained, provable. And if the model hallucinated `4200` — misread a table,
averaged two invoices, transposed a digit — every one of those controls works
perfectly while the wrong amount leaves the building. The rollback is clean. The
audit is intact. The customer is still wrong.

None of them ask the only question that matters: **where did that number come
from?**

```python
from agent_saga import ProvenancePolicy, Provenance, sourced, user_value

policy = ProvenancePolicy()
policy.require("stripe.charge", "amount", Provenance.SOURCED)
policy.prohibit("wire.transfer", "this deployment never initiates wires")

policy.check("stripe.charge", {"amount": sourced(4200, receipt)}, broker=broker)
```

Four levels, ordered: `SOURCED` (traceable to a receipt that resolves right
now) > `USER` (verbatim in the human's request) > `DERIVED` (computed from
sourced values, with the derivation stated) > `MODEL` (invented).

Two properties do the work:

- **Untagged means `MODEL`.** Absence of provenance is not evidence of
  provenance. Defaulting the other way would mean the one argument someone
  forgot to tag is exactly the one that sails through.
- **`SOURCED` is verified, never accepted.** You cannot just *label* a value
  sourced. The gate re-hydrates the receipt against the document and checks the
  value actually appears in the span it claims. A document that drifted, or a
  number that isn't in it, downgrades to `MODEL` and is refused like any other
  invention.

`policy.as_gate_rule()` plugs into `PreFlightGate`, so provenance is enforced on
the same path as budgets and approvals rather than as a second thing everyone
remembers separately.

**On misuse, plainly:** `prohibit()` is an operator control, not a guarantee
against a determined misuser — anyone who controls the process can edit the
policy. What it provides is a declared, auditable boundary: a refusal recorded
*before* the effect, and a log a reviewer can check afterwards. No library makes
a person honest. This one makes what they did legible.

## Making a cheap model produce answers you can trust

Nothing in software makes a 7B model smarter — that lives in the weights. What
you can stop doing is *trusting* it, and start *checking* it, then escalating on
the evidence when the check fails.

```python
from agent_saga import cascade, tools_must_exist

result = await cascade(request,
                       ladder=[local_7b, mid_tier, frontier],
                       verify=tools_must_exist(my_tool_names))

result.host                     # which tier produced the verified answer
result.escalations              # how often the cheap one wasn't enough
result.tokens_used              # across ALL tiers, including rejected ones
print(result.format_text())
```

The 7B answers first. Its output goes through a check that can actually fail —
a schema, a grounding receipt, the tool registry, an entailment judge. Pass, and
you paid 7B prices for a verified answer. Fail, and the next tier up receives
the request **plus the specific reason the last one was rejected**, which is the
part that measurably helps rather than just costing more.

```
cascade resolved on frontier after 1 escalation(s)
  ->  local-7b            110 tok  0.01s  rejected: called tool(s) that do not
                                            exist: ['send_emial']
  ok  frontier            550 tok  0.04s
  total tokens across all tiers: 660
```

Three deliberate properties:

- **Escalation is evidence-driven, not confidence-guessed.** Asking a model how
  sure it is yields a number weakly correlated with being right. Asking "does
  this tool exist?" yields a fact. Only facts escalate here.
- **Nothing unverified is ever returned.** If no tier passes, it raises
  `CascadeExhausted` with the full trace. Returning the last answer anyway would
  make the cascade decorative.
- **The saving is measured.** Rejected tiers' tokens are counted too — counting
  only the winner would flatter the cascade. If it isn't saving you anything,
  the report is where you find that out.

The whole ladder, failures included, lands in the WAL as `CASCADE_RESOLVED`.

## Already have an agent? Adopt it in one command

```bash
agent-saga adopt --out saga_tools.py
```

It indexes your project, detects which frameworks you're using, finds your
tools, and writes the wrapping module for them. What it will **not** do is
decide each tool's semantics — that judgement is what the whole engine rests
on, and a plausible default there is the most expensive kind of wrong. An
emailer quietly marked `COMPENSABLE` looks protected and isn't.

So every tool comes out as `semantics=DECIDE`, and `DECIDE` is a sentinel that
raises. **The generated module cannot even be imported until a human has
classified each side effect.** That's deliberate friction in the one place
friction pays.

It does offer hints, clearly labelled as hints:

```
  app.tools.send_welcome_email  (decorated, line 9)
      hint: the name contains 'send', which usually means no automated undo
      exists -- consider IRREVERSIBLE, which is gated before it runs rather
      than undone after. A hint, not a classification.
```

## The 30-step problem

A step that succeeds 98% of the time is a good step. Chain thirty of them and
the workflow completes **54.5%** of the time — that's just `0.98 ** 30`, and
you can check it in a REPL.

No safety layer changes that arithmetic. What a transaction boundary changes is
what the other **45.5%** leaves behind: a charge with no order, a server with no
owner, a half-written record nobody knows about. The question isn't whether your
agent will fail partway — at thirty steps it's a coin flip — it's whether the
failure is *recoverable* or an incident.

Almost nobody verifies that, because verifying it means deliberately breaking
your own workflow at every step and inspecting the world afterwards. So:

```python
from agent_saga import prove_rollback

proof = await prove_rollback(scenario, snapshot=read_world, reset=clear_world)
assert proof.proven      # every failure point unwinds to a clean world
```

It runs your workflow once per step, failing at a different step each time, in
both shapes — the step never ran, and the step ran *then* failed (the `UNKNOWN`
case, where a timed-out call may well have landed). After each run it compares
the actual world to its starting state.

The part that matters: **it checks the world, not the engine's opinion of it.**
A compensation that runs, returns successfully, and undoes nothing produces a
`RollbackReport` saying `clean` and a world that isn't. That's reported as
`ENGINE_DISAGREES`, and it's the only check in the package that catches a
compensation which lies:

```
rollback proof: 6 probe(s) across 3 step(s)
  ok   step 1 stripe.charge [fail before]
  LIES step 1 stripe.charge [fail after]
         left behind: charges: [] -> ['ch_1']
```

In CI, via the pytest plugin:

```python
async def test_onboarding_is_safe(assert_rollback_proven):
    await assert_rollback_proven(scenario, snapshot=world.snapshot, reset=world.reset)
```

## See it in 5 seconds

```bash
pip install agent-saga && agent-saga demo
```

Three acts, no network, no configuration, no API keys. Everything in it is the
real engine — a real write-ahead log, a real gate, real compensations, a real
killed process.

| | What happens | What you're left with |
|---|---|---|
| **Act I** | An ordinary agent charges a card, launches a server, then step 3 fails | `charges: ['ch_1']`, `servers: ['i-1']` — **money taken, server running, nobody coming to clean up** |
| **Act II** | The *same three calls* inside `saga_scope`. Same failure | `charges: none`, `servers: none` — unwound LIFO, and the report says `clean` (read from `RollbackReport`, not hardcoded) |
| **Act III** | The same saga, but the process is **killed mid-transaction** — `os._exit()`, skipping `finally`, `atexit`, and loop shutdown | The charge is refunded anyway, **by a different process**, from the log alone |

Act III is the one worth watching twice. A `try/except` stack keeps its
compensations in memory and dies with the process. A LangGraph checkpoint
restores your *agent's* state — it cannot un-charge a card. Here the intent was
fsynced **before** the effect fired, so recovery never depended on the process
that caused the mess still being alive. The daemon also waits for the dead
worker's **lease** to expire rather than checking a PID, because PIDs are reused
within minutes and an expired lease is the only thing that actually proves an
owner is gone.

```bash
agent-saga demo --logs      # same run, with the engine's own trace
agent-saga demo --no-color  # plain text for CI or piping
```

Then draw what you just watched:

```bash
agent-saga graph --wal ./agent-saga.wal     # the rollback fork, as Mermaid
agent-saga animate --wal ./agent-saga.wal -o rollback.svg
```

`graph` gives you the *shape* of the unwind. `animate` gives you its **order** —
one self-contained SVG in which the forward path builds downward, then the
compensations fire back up it in reverse, one step at a time, ending on a
verdict banner. No JavaScript, no network, no dependencies: it renders inside
`<img src=...>`, in a GitHub comment, in a PDF postmortem, and under a strict
Content-Security-Policy. It carries its own `prefers-reduced-motion` block, so
a viewer who asked their OS for less motion gets the final frame immediately
instead of nothing.

Both renderers consume the *same* reconstruction of the log, so a diagram and
an animation of one WAL cannot disagree — and neither will draw a partial
rollback as if it were clean. An orphaned effect gets its own colour, its own
label, and a counted line in the verdict.

```bash
agent-saga animate --wal ./prod.wal --saga a1b2c3 --theme light --no-loop -o incident.svg
```

### Whole-log visuals

`animate` renders one saga. Three properties only exist *across* a log, and a
terminal genuinely cannot show them:

```bash
agent-saga viz --wal ./prod.wal --kind fleet    -o fleet.svg
agent-saga viz --wal ./prod.wal --kind chain    -o chain.svg
agent-saga viz --wal ./prod.wal --kind outcomes -o outcomes.svg
```

- **`fleet`** — every saga as a bar on real elapsed time. Which ran at once,
  which overlapped, where in the wall clock the rollbacks landed. Rolled-back
  spans carry a hatch stripe as well as a colour, so they survive greyscale and
  a red/green colour deficiency.
- **`chain`** — the hash chain, **verified while drawing**. Each record names
  the SHA-256 of its predecessor; a link that doesn't match is drawn red and
  labelled `BROKEN`, the header reports the count, and when a long log is
  cropped to fit, every break is kept. A tamper-evidence graphic that drew a
  doctored log as intact wouldn't be a flawed graphic — it would be a forgery
  aid. A log written with `chain=False` is reported as *unchained*, never as
  intact, and a partly-chained one says so too.
- **`outcomes`** — tool × outcome. Which calls the world kept and which had to
  be taken back. Below five observations a cell shows its raw count and greys
  out: "100% orphaned" over one call is a number that means nothing and reads
  as though it means everything. Same discipline [`risk.py`](agent_saga/risk.py)
  applies to lift.

### How the project page is built

Five of the figures on [the project page](site/index.html) are rendered by the
commands above, by [`site/build_assets.py`](site/build_assets.py), from
write-ahead logs of real runs — the rollback pair from two sequential scenarios,
the fleet/chain/outcome trio from nine sagas racing through one WAL.

To be exact about the claim, since it is the kind that is usually inflated: the
**page's data visuals are generated by agent-saga; the page's layout, shader
and motion are hand-written**. agent-saga is a transactional-safety library,
not a web framework, and it would be a worse library if it tried to be one.

What the generation buys is not convenience, it is that the page cannot lie:
[`tests/test_site_assets.py`](tests/test_site_assets.py) regenerates the
sequential figures and compares them byte-for-byte, checks the concurrent ones
still carry an intact chain and real aborts, and pins the page's numbers
against the repository — the adapter count against `agent_saga/adapters/`, the
321 interleavings by *running* the verifier, "0 dependencies" against
`pyproject.toml`. If compensation ordering changes, or `clean` gets more
generous, CI fails instead of the home page quietly showing behaviour that no
longer exists.

More worked examples:

```bash
python examples/multi_domain.py         # 5 systems, 1 transaction, no network needed
python examples/chaos_demo.py           # optimistic vs. transactional, side by side
python examples/small_model_big_docs.py # a 2k-token host works a 440k-char document
```

What agent-saga does **not** claim: it does not make any model smarter. It makes
whatever model you have budgeted, audited, repaired once when it emits malformed
actions, stopped when it thrashes, and rolled back when it fails. 📖 The runtime
reference: [docs/AGENT_RUNTIME.md](docs/AGENT_RUNTIME.md).

## Build an enterprise agent app in one command

Every part of a production agent app ships here. What used to be missing was the
**assembly** — and assembly is where the expensive mistakes live, because the
dangerous choices aren't errors, they're laptop defaults that stay silent in
production.

```bash
agent-saga new myagent && cd myagent
pytest                                  # a rollback test that passes on day one
agent-saga doctor --replicas 3          # posture check; exit 1 on blockers
```

The scaffold is ~10 files you can read in one sitting: a FastAPI service with a
`/readyz` probe that **fails closed** so a misconfigured replica never joins the
load balancer, tools that declare their semantics and register their inverses by
name, env-driven WAL selection, a Dockerfile and compose file, and a test that
proves the rollback actually runs. Anything the template can't decide for you is
left as a `TODO` rather than a plausible default — a plausible default in a
safety boundary is how a team ends up trusting a control nobody chose.

`agent-saga doctor` audits the three postures that silently lose effects:

| Finding | Why it costs money |
|---|---|
| `wal-shared` | A file WAL behind a load balancer: pod A's log isn't on pod B's disk, so a dead pod's orphans have no daemon that can see them. A **risk** at one replica, a **blocker** at more — that's arithmetic, not opinion |
| `compensations-recoverable` | `compensate=lambda: refund(id)` rolls back beautifully until the process dies and the closure dies with it. Only a registry-named handler with JSON kwargs survives |
| `wal-backpressure` / `wal-dropped` | Under `DROP_SILENT`, a record that never lands is a side effect that can never be recovered — the engine's one unrecoverable failure mode, chosen by a config flag |

## The boilerplate, and what it costs now

Writing a compensation factory for every tool was the most-cited friction with
this library, and it was fair. Declaring **semantics** is load-bearing and
stays — the engine will not guess whether an effect is reversible. Declaring
the *inverse* was ceremony, and mostly the same shape every time:

```python
# before — repeated at every call site
charge = kit.safe_tool(
    stripe_charge, semantics="COMPENSABLE",
    compensate=lambda r: Compensation(fn=refund_charge, handler="stripe.refund",
                                      kwargs={"charge_id": r["id"]},
                                      description=f"refund {r['id']}"))

# after — declared once, next to the function that does the undoing
@inverse_of(stripe_charge, maps={"charge_id": "id"})
@compensator("stripe.refund")
def refund_charge(charge_id: str): ...

charge = kit.safe_tool(stripe_charge, semantics="COMPENSABLE")   # pairing found
```

`delete_by(fn)` covers the commonest shape (created something, returned its id),
and `call_with(fn, **kwargs)` covers inverses whose target is known before the
forward call — which stays correct even on an `UNKNOWN` outcome.

Three things the shorthand refuses to trade away: it never infers semantics; it
raises at import if the inverse isn't registered with `@compensator` (a closure
can't be run by the recovery daemon after a crash); and if the forward result
lacks the field the inverse needs, it raises naming both what it wanted and what
it saw, rather than compensating with `None`. An explicit `compensate=` always
wins.

## State across a rollback — the honest trade-off

A saga has no isolation. That is the deal it makes in exchange for not holding
a database transaction open across an LLM's thinking time: each step commits as
it goes, so intermediate state is briefly visible to other readers. No library
removes that; what agent-saga does is make it *legible*.

| Kind of state | Use | Why |
|---|---|---|
| Private to the saga (a dict, a scratch object) | `reversible()` in [snapshot.py](agent_saga/snapshot.py) | Captures a deep copy before the mutation; the inverse is derived for you and stays correct even on an `UNKNOWN` outcome |
| Durable and shared (a row, a charge, a file) | `@compensator` + `@inverse_of` | Survives `kill -9`, so `saga-recoveryd` can finish the unwind from another process |
| Visible mid-flight to other readers | [patterns/tentative.py](agent_saga/patterns/tentative.py) | Marks the resource `PENDING` until it resolves exactly once to `COMMITTED` or `ROLLED_BACK` |
| Spanning days or chat sessions | [durable_memory.py](agent_saga/durable_memory.py) | Long-lived transaction state an agent can query across sessions |

The one architectural decision worth making up front is which of those four a
piece of state is. Get that right and the rest follows; get it wrong and you'll
find out at rollback, which is the expensive time to find out.

## Surgical repair — fix one step, keep the rest

All-or-nothing is the right default and the wrong only option. A six-step
onboarding that fails at step four because the model produced a malformed
postcode does not need the payment reversed and the account deleted. It needs
the postcode fixed and steps five and six run.

```python
from agent_saga import RepairSession

session = RepairSession.open(records, saga_id,
                             operator="ops@example.com",
                             reason="malformed postcode from the model")
print(session.format_text())          # what's kept, what failed, any blockers
session.amend(postcode="SW1A 1AA")    # recorded, with the old value
await session.resume(continuation, wal=wal)
```

Three things make this an escape hatch rather than a hole in the guarantee:

**It refuses unless the retained steps could still be undone.** Resuming keeps
the effects of steps 1..k-1. If the resumed part then fails, those steps must
still be reversible — otherwise repairing has *manufactured* the orphan the
engine exists to prevent. So a session blocks unless every retained step has a
registry-backed compensation (a handler name and JSON kwargs any process can
run), and it names each offending step rather than degrading into a warning.

**Resume inherits the past.** The continuation runs in a fresh saga whose stack
is pre-loaded with the retained steps, compensations rebuilt from the log. A
failure after the repair unwinds the *whole* transaction — there's a test
asserting `crm.create_account` and `stripe.charge` both roll back when the
resumed step fails.

**The hatch is audited, because an unaudited hatch voids the audit.** `operator`
and `reason` are required, not defaulted. Amendments record the *before* value —
"changed the postcode" and "changed the amount from 50 to 5000" are different
events, and only one is routine. `REPAIR_OPENED`, `REPAIR_RESUMED`, and
`REPAIR_ABANDONED` land on the same hash-chained log as everything else.

The two realistic entry points are a saga whose process died (no terminal
record, effects still standing) and `agent-saga quarantine`, which freezes a
saga deliberately without unwinding it.

## Do you actually need this?

**If your agent calls two or three tools that touch nothing durable — no money,
no infrastructure, no messages to real people — a plain `try/except` is enough,
and this library is overhead.** That's a real answer, not false modesty.

What a `try/except` stack cannot do is survive the process dying. It holds
compensations in memory, so `kill -9` takes them with it and the charge stays
charged. It also has no notion of an `UNKNOWN` outcome (a timed-out charge may
well have landed), no idempotency key when a compensation is retried, no way to
distinguish a clean unwind from a partial one that needs a human, and no gate
that refuses an irreversible action *before* it happens. Reach for agent-saga at
the point where a failure halfway through leaves something real behind that
someone would otherwise clean up by hand.

## One saga across frameworks that never heard of each other

A CrewAI researcher reserves budget. A LangGraph writer publishes the draft. An
AutoGen reviewer sends the notification — and the notification fails. Which
framework un-publishes the draft?

None of them. They can't: each knows only its own graph, so the cleanup falls to
whoever is on call with three dashboards and a guess.

```bash
python examples/cross_framework.py
```

```
    [CrewAI]    reserved budget    -> res_1
    [LangChain] published draft    -> doc_1
    [AutoGen]   FAILED: notification service unreachable

  One boundary unwinds all three, last in first out:
    <- reviewer.notify
    <- writer.publish_draft
    <- research.reserve_budget
  rollback reported: clean

  The log proves it was one transaction, not three:
    distinct saga ids : 1
```

**There is no bridge, no registry, and nothing to configure.** `saga_scope` sets
a **contextvar**, and every adapter's runner ([`adapters/_common.py`](agent_saga/adapters/_common.py))
checks `current_saga()` before doing anything. A wrapped tool joins whatever
saga is already open, whichever framework called it — which is exactly why it
works with frameworks that have never heard of each other.

### For production: `SagaFleet`

The contextvar has two silent failure modes, and both bite at scale. Measured,
not assumed:

```
in the coroutine        : True
asyncio.to_thread       : True      (copies the context)
raw threading.Thread    : False
ThreadPoolExecutor      : False
```

CrewAI and AutoGen dispatch tools through their own executors. When they do,
`current_saga()` is `None` on that thread and the ordinary runner performs the
side effect **with no WAL record and no compensation** — while every log line
still says the tool is saga-aware. The second failure mode: register twelve
tools, miss one, and nothing tells you until its effect needs undoing.

[`SagaFleet`](agent_saga/fleet.py) closes both:

```python
fleet = SagaFleet("content-pipeline")
reserve = fleet.register(crew_call, framework="crewai",    name="research.reserve",
                         semantics=COMPENSABLE, compensate=release)
publish = fleet.register(lc_call,   framework="langgraph", name="writer.publish",
                         semantics=COMPENSABLE, compensate=unpublish)

fleet.assert_fully_covered()          # CI gate: nothing unwindable was missed

async with fleet.transaction(wal=wal):
    await reserve(amount=5000)
    await publish(title="Q3")         # same saga, different framework, any thread
```

- **Re-attaches** the active saga inside a framework's own thread, so the
  boundary survives the executor
- **Refuses** to run a registered tool outside a boundary (`BoundaryRequired`)
  rather than performing an unprotected effect
- **`coverage()`** — a machine-readable manifest of which tools are protected,
  from which framework, and which have a recoverable inverse versus a closure
  that dies with the process

One honest limit, documented in the module: the fleet moves the saga's
*identity* across threads, not the event loop that owns it. A coroutine
touching the saga must be scheduled back with
`asyncio.run_coroutine_threadsafe(coro, loop)`; starting a second loop with
`asyncio.run()` on a worker stalls until the WAL barrier times out.

## Use it *with* LangGraph, not instead of it

A checkpoint restores your **agent's** state. It does not un-send the wire
transfer. LangGraph's time-travel rewinds the graph so you can re-run a node;
agent-saga calls the refund. They compose — and there's an adapter for exactly
that:

```python
from agent_saga.adapters.langgraph import wrap_tool, saga_run
from agent_saga import ActionSemantics, Compensation

charge = wrap_tool(stripe_charge_tool,          # same name, description, schema
                   semantics=ActionSemantics.COMPENSABLE,
                   compensate=lambda r: Compensation(
                       fn=refund, handler="stripe.refund",
                       kwargs={"charge_id": r["id"]}))

result = await saga_run(graph, {"input": "..."})   # graph raises -> LIFO unwind
```

The wrapped tool is a drop-in: the model and the graph see no difference, and
outside a saga it calls straight through, so the same object works in a script
or a unit test. Adapters also ship for CrewAI, AutoGen, LlamaIndex, OpenAI
Agents, and MCP.

## Grounded answers (v0.4.2) — a hallucination cannot pose as a sourced fact

The enterprise problem with LLM chat is not that models are insufficiently
brilliant — it is that a wrong answer and a right answer arrive in the same
confident voice. No middleware makes a model hallucinate less. What agent-saga
does, mechanically, is make every claim in an answer **wear its evidence or
wear a label**:

```python
from agent_saga import ContextBroker, ground

broker = ContextBroker()
spans = broker.add_document("runbook", open("runbook.txt").read())
sid = broker.admit_summary("the pressure limit is 4200 kPa", spans)  # receipts verified

answer = f"The limit is 4200 kPa [{sid}]. It was installed in 2019, I believe."
report = ground(answer, broker)

report.claims[0].status    # "VERIFIED"  — citation resolves right now, hashes match
report.claims[1].status    # "UNCITED"   — the model's own assertion, labeled as such
report.fully_grounded      # False — one unlabeled claim spoils it, by design
print(report.format_annotated())   # the answer wearing its labels, for a reviewer
```

Statuses are exact: `VERIFIED` (cites a live summary whose SHA-256 receipts
resolve *right now*, and every direct quote appears in those sources),
`UNCITED` (unevidenced assertion), `BROKEN_CITATION` (cites something never
admitted, evicted — the eviction reason travels into the verdict — or whose
receipts stopped resolving), `BROKEN_QUOTE` (an invented quote can't hide
behind a valid citation), `UNSUPPORTED` (your optional entailment hook — an
LLM judge through the `Router`, an NLI model — rejected the claim). The verdict
lands in the WAL as `ANSWER_GROUNDED`, so the audit chain runs end to end:
what the model saw, why it acted, what its answer could and could not prove.

Your policy decides what unlabeled claims may touch: block `UNCITED` numbers
in financial reports, require `VERIFIED`-only in clinical summaries, page a
human on anything `BROKEN_*`.

## What the write-ahead log makes possible

Every agent framework logs. agent-saga's log records something none of the
others do: **whether the effect had to be undone.** That single fact is ground
truth nobody else collects, and four capabilities fall out of it.

```python
from agent_saga import (build_corpus, counterfactual_replay,
                        WALProfile, synthesize, FailureModel)
```

**Training data, labelled by reality** — `build_corpus(records)`. Every action
labelled by whether the world kept it. The hard part is blame: when step 5
fails, steps 1–4 are rolled back *and they were correct*. Labelling them
negative teaches a model to avoid the calls that worked, so they're
`COLLATERAL` and excluded. `preference_pairs()` emits DPO-shaped pairs matched
within a tool.

**Try a cheaper model on real history, risk-free** — `counterfactual_replay()`.
The log is a simulator of that afternoon's world. Point a candidate at it,
serve recorded results, and nothing executes — the replay context never invokes
a forward callable at all. When the candidate diverges, the answer is
`UNKNOWABLE`, not a guess: the recording genuinely has no result for the call it
made.

**Share your traffic without sharing your customers** — `WALProfile.fit()` /
`synthesize()`. The profile keeps shapes and ranges, never a value a person
typed, so *the profile itself* is shareable. Fifty real sagas become fifty
thousand synthetic ones for load-testing recovery. Every record is marked
`__synthetic__` — an audit log indistinguishable from a real one is an
instrument for fabricating evidence, not a test fixture.

**A gate that gets better the longer you run** — `FailureModel.fit()`. Learns
which call shapes had to be undone. Reports **lift over the base rate**, because
"9 of 12 failed" is meaningless when three quarters of everything fails. Below
its support threshold it produces no number and says silence isn't a clean bill
of health. It ships no automatic blocking gate — a correlation is grounds to
look, not to refuse.

## Proof, not adjectives

```bash
python -c "import asyncio; from agent_saga import verify_rollback_invariants; print(asyncio.run(verify_rollback_invariants(max_steps=6)).format_text())"
```

**321 failure interleavings. Seven invariants. Zero violations.**

`verify_rollback_invariants()` enumerates *every* failure shape up to N steps —
which step's forward call raises × which subset of inverses then refuse ×
whether a committed step has no inverse — and runs each against the real engine,
not a model of it. LIFO order, no double compensation, bounded retries, exactly
one outcome bucket per step, `clean` never claimed over an incomplete rollback,
halt stranding only earlier steps, orphans reported rather than dropped.

And it states its bound: this is bounded model checking, **not** a proof for
unbounded N and not a claim about concurrency. "Verified" with no bound attached
is the kind of claim this project exists to avoid.

Benchmarks come with their methodology, in [docs/BENCHMARKS.md](docs/BENCHMARKS.md):
fast path ~17 µs, durable path ~7.9 ms, reported separately and never blended —
and the WAL measured against your device's own `fsync` floor, so the number that
travels between machines is agent-saga's **marginal** cost: 0.97 ms, 27%.

## It plugs into what you already run

Spans carry **OpenTelemetry GenAI semantic conventions** (`gen_ai.system`,
`gen_ai.request.model`, `gen_ai.usage.*`, `gen_ai.tool.name`, `error.type`) —
so Langfuse, Arize Phoenix, Datadog LLM Observability, Grafana and Honeycomb
render agent-saga traces in the panels already built for them, with **zero
configuration**. The `saga.*` attributes stay alongside, because compensation
semantics have no equivalent in the conventions.

Prompt and argument capture is **off by default**: a tracing backend is usually
a third party nobody classified as a data store.

**13 framework adapters** — LangGraph, CrewAI, AutoGen, LlamaIndex, OpenAI
Agents, **Semantic Kernel**, **Vertex AI**, Temporal, Camunda, SQLAlchemy,
Supabase, and the one-line `wrap_*` wrappers. The public API is `wrap_tool` in
every one.

## Does it have X? — the capability matrix

Everything below is shipping, tested code with a module you can open. The core
has **zero required dependencies**; the Install column shows what an optional
backend needs.

| Capability | Where | Install |
|---|---|---|
| **Async-native execution** — `saga_scope`, `ctx.execute`, every WAL backend, the recovery daemon. Sync tools are auto-offloaded to a bounded thread pool so a blocking client can't stall the loop | [context.py](agent_saga/context.py), [executors.py](agent_saga/executors.py) | core |
| **Auto-compensating fallbacks** — alternate tool paths, parameter repair, and adaptive retries *before* any rollback; a step that recovers is marked `COMPLETED_VIA_FALLBACK` | [healing.py](agent_saga/healing.py), [retry.py](agent_saga/retry.py) | core |
| **Pivot instead of unwind** — `fallback_action` on `ctx.execute`: if the hotel fails, book a different hotel and keep the flight | [context.py](agent_saga/context.py) | core |
| **Tentative state** — a resource touched mid-saga is visibly `PENDING` until it resolves exactly once to `COMMITTED` or `ROLLED_BACK` | [patterns/tentative.py](agent_saga/patterns/tentative.py) | core |
| **Durable state persistence** — four WAL backends: file, memory-mapped, Postgres, Redis | [wal/](agent_saga/wal) | `[postgres]`, `[redis]` |
| **Distributed locks** — cross-process semantic locks with heartbeat leases and deadlock timeouts | [locks.py](agent_saga/locks.py) | `[redis]` |
| **Distributed limits & approvals** — rate/spend caps and M-of-N multi-sig human gates, shared across processes | [limits.py](agent_saga/limits.py), [approvals.py](agent_saga/approvals.py) | `[redis]`, `[postgres]` |
| **Crash recovery daemon** — replays the WAL after `kill -9`; expired leases (not PIDs) prove the owner is gone; deterministic tokens make double-compensation structurally impossible | [recovery.py](agent_saga/recovery.py) | core |
| **Visual debugging** — `agent-saga ui` time-travel debugger over a WAL | [ui/](agent_saga/ui) | core |
| **Graph export** — `agent-saga graph` renders the forward path *and the rollback fork* as Mermaid or Graphviz DOT | [graph.py](agent_saga/graph.py) | core |
| **Animated export** — `agent-saga animate` renders the unwind as a self-contained animated SVG: no JS, no network, CSP-safe, `prefers-reduced-motion` aware, deterministic bytes | [animate.py](agent_saga/animate.py) | core |
| **Whole-log visuals** — `agent-saga viz` draws the fleet timeline, the hash chain (**verified while drawing**), and the tool × outcome matrix | [viz.py](agent_saga/viz.py) | core |
| **BPMN 2.0 import/export** — round-trip to visual workflow tooling | [bpmn.py](agent_saga/bpmn.py) | core |
| **MCP integration** — a saga proxy in front of any MCP server, with policy, `inputSchema` tool declarations, and MCP-dispatched compensations | [mcp/](agent_saga/mcp) | core |
| **Framework adapters** — LangGraph, CrewAI, AutoGen, LlamaIndex, OpenAI Agents, SQLAlchemy, Supabase | [adapters/](agent_saga/adapters) | per-framework |
| **Multi-model routing** — one IR over any host (local 7B → frontier API); every decision and refusal names every candidate and reason | [ir.py](agent_saga/ir.py), [router.py](agent_saga/router.py) | core |
| **Dry-run / simulation** — `PREVIEW` semantics, speculative pre-flight plans, `--dry-run` recovery sweeps, and chaos fault injection | [preview.py](agent_saga/preview.py), [chaos.py](agent_saga/chaos.py), [testing.py](agent_saga/testing.py) | core |
| **Tamper-evident audit** — hash-chained WAL, `agent-saga verify`, WORM export, selective-disclosure Merkle proofs | [integrity.py](agent_saga/integrity.py), [provenance.py](agent_saga/provenance.py), [vault.py](agent_saga/vault.py) | core |
| **Multi-tenancy** — tenant-scoped WALs, limits, approvals, and snapshots via contextvars | [tenant.py](agent_saga/tenant.py) | core |
| **Observability** — OpenTelemetry spans, OTLP export, LangChain callbacks | [observability/](agent_saga/observability) | `[otel]` |
| **Encryption at rest** — keyring-based WAL encryption | [encryption.py](agent_saga/encryption.py) | `[encryption]` |
| **Testing tools** — a `pytest-agent-saga` plugin and chaos harness | [pytest_plugin.py](agent_saga/pytest_plugin.py) | core |
| **Project scaffold** — `agent-saga new <name>` emits a runnable enterprise app: FastAPI service, fail-closed `/readyz`, registered compensators, Docker, and a passing rollback test | [scaffold.py](agent_saga/scaffold.py) | core |
| **Production readiness audit** — `agent-saga doctor` names the postures that silently lose effects; exit 1 on blockers, `--strict` for CI | [readiness.py](agent_saga/readiness.py) | core |
| **Scope-correct refactoring** — `agent-saga refactor rename` builds a symbol/reference graph, computes the blast radius, shows a diff, and applies it as a transaction. Locals, strings, class attributes, and aliased imports are never mis-renamed | [codemod/index.py](agent_saga/codemod/index.py), [codemod/plan.py](agent_saga/codemod/plan.py) | core |

Draw what actually happened, straight from a log:

```bash
agent-saga graph --wal ./agent-saga.wal            # Mermaid, pastes into markdown
agent-saga graph --wal ./agent-saga.wal --format dot | dot -Tpng -o saga.png
```

The rollback fork is the point: `compensated` (clean), `COMPENSATION FAILED —
needs a human`, and `ORPHANED — no undo exists` render as three visually
distinct outcomes. A partial rollback can never draw like a clean one.

## The 30-second version — Universal Agent Engine & `AgentKit` (v2.0.1)

The whole engine behind one object. Wrap a tool once, run work in a transaction,
and — the part that's new — let the agent *read what it's guaranteed*.

```python
from agent_saga import AgentKit

kit = AgentKit(name="my-agent")

charge = kit.safe_tool(stripe_charge, semantics="COMPENSABLE",
                       compensate=lambda r: {"handler": "refund", "kwargs": {"id": r["id"]}})

async with kit.transaction():        # a saga boundary
    await charge(amount=4200)        # gated, logged, refunded on any failure
    await ship_order(...)            # if this throws, the charge is rolled back

kit.guarantees()   # machine-readable manifest: exactly what is enforced, versioned
kit.status()       # live posture — fail-closed if a safety signal can't be read
```

`safe_tool` is zero-ceremony: the same wrapped callable runs untouched outside a
transaction, so it works identically in a script, a notebook, or a test. Reach
for the lower-level `saga_scope` / `Compensation` API (below) when you need full
control; `AgentKit` is the ergonomic front door onto the exact same guarantees.

📖 **[MANUAL.md](https://github.com/thomasjgeorge23/agent-saga/blob/main/MANUAL.md)** — the complete reference: every subsystem, how it
works and why, CLI and configuration, deployment checklists, and troubleshooting.

---

## Core Principles & System Architecture

### 1. Auditable Consistency Over Post-Hoc Cleanup
> *“A bank does not buy a post-disaster cleanup script — it implements a control that refuses to cross an uncompensable boundary without a human on the hook.”*

`agent-saga` is not an optimistic undo utility; it is an auditable transactional safety boundary designed to prevent unrecoverable side effects when autonomous systems execute real-world operations.

### 2. Runtime-Derived Compensations (The Temporal Wedge)
In traditional orchestrators like Temporal, compensating steps are statically declared at authoring time. But when autonomous agents select tools dynamically at runtime, the inverse operation cannot be statically assumed up front.

**The Invariant Rule**: The compensating action can only be derived *after* the forward step completes and returns concrete runtime state parameters (e.g., specific row IDs, charge tokens, or message handles).

### 3. Strict Rollback Transparency (`clean` vs. `partial`)
Swallowing partial rollback failures is the leading cause of silent data corruption in transactional software. Callers and compliance operators must always be able to distinguish a 100% clean state restoration (`RollbackReport.clean == True`) from a partial/dirty failure that demands human intervention.

### 4. Pre-Flight Enforcement Over Post-Disaster Recovery
High-risk or non-compensable actions (e.g., sending emails, executing wire transfers above thresholds, or permanent disk operations) evaluate policy gates *before* any local database row or external API side-effect is modified.

### 5. Deterministic Fail-Closed Invariants
Systems processing Write-Ahead Logs (WAL) must never swallow corrupted records or unreadable state. If a background recovery daemon encounters an unparseable log entry or missing decryption key, it halts immediately and alerts — preventing silent state drift.

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

## Circuit breaker

Limits cap what an agent is *allowed* to do. This caps what's *worth* doing:
when Stripe has been timing out for ninety seconds, the hundredth charge attempt
won't succeed either — and each one costs a saga, a WAL fsync, a compensation of
unknown outcome, and thirty seconds of an agent's life.

```python
from agent_saga import CircuitBreaker, BreakerPolicy, set_breaker

set_breaker(CircuitBreaker(BreakerPolicy(
    failure_threshold=5,      # consecutive failures, or…
    failure_rate=0.5,         # …50% of calls failing,
    min_volume=10,            #    once there's enough volume to mean anything
    window=60,
    cool_down=30,             # then one trial call
), wal=wal))
```

It's the only control here that needs **outcome feedback**, which is why it
couldn't be built alongside the budgets: a limit decides before the call and
never learns what happened; a breaker is nothing but what happened.

Three behaviours where the obvious answer is wrong:

- **A refusal is not a failure.** Over budget, blocked by policy, denied by a
  human — those mean the system *worked*. Counting them would trip the breaker
  exactly when the controls were doing their job, and it would then block the
  calls that were still fine. Only a tool that actually ran and raised counts.
- **A breaker never blocks a rollback.** If the forward path is failing, the
  compensations are precisely what you need to run. Blocking them because their
  connector looks sick would strand money mid-transaction — turning a dependency
  outage into a financial one. Compensation failures don't feed it either, or it
  would open on the connector whose refunds are failing and then refuse the rest
  of them.
- **This one fails *open*, and that isn't an inconsistency.** A budget that can't
  be verified must refuse, because failing open means overspending. A breaker is
  an *availability* protection: refusing all work because its own store is down
  would be an outage it invented. It degrades to per-process state and keeps
  going. The rule throughout: **fail toward the behaviour you'd have without the
  feature** — for a budget that's "refuse", for a breaker it's "make the call".

Checked before limits, so a call to a dependency known to be down doesn't consume
budget on its way to being refused. Trips, probes and recoveries land in the
hash-chained WAL.

---

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

Every shipped connector registers a reconciler alongside its compensator, so
importing the connector gets both:

```bash
agent-saga reconcile --wal-path ./agent-saga.wal \
    --import agent_saga.connectors.stripe \
    --import agent_saga.connectors.postgres
```

| Handler | What it re-reads | The failure it catches |
|---|---|---|
| `stripe.refund` | the charge | refund acknowledged but never applied; **partial refunds**, which are not reversals |
| `postgres.restore_row` | the row's columns | the UPDATE reported a rowcount but a trigger rewrote it; a third party wrote in between (reported as *indeterminate*, not guessed) |
| `postgres.delete_inserted_row` | row existence | the inserted row is still there |
| `postgres.reinsert_row` | row + values | a row with the right key came back carrying the *wrong* values |
| `salesforce.revert_object` | the patched fields only | the PATCH returned 204 and a workflow rule immediately put the value back |

That last one is worth dwelling on: a Salesforce workflow rule, flow, or Apex
trigger can rewrite a field microseconds after your update, and *nothing in the
API response tells you*. Only reading the record does. Only the fields the saga
touched are compared — reverting a record is not a claim about the rest of it.

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

**Rollback-safety certificates.** Chain-intact proves the log wasn't *altered*;
it doesn't prove the agent left nothing stranded. `certify` reads a log and
proves the stronger property — that every committed effect was accounted for
(compensated, or explicitly terminal) — and names the exact step if one wasn't.
It returns non-zero, so a release that could strand an uncompensated charge fails
CI instead of shipping.

```bash
agent-saga verify  --wal-path ./agent-saga.wal   # the log was not altered
agent-saga certify --wal-path ./agent-saga.wal   # every effect is accounted for
```

**Selective-disclosure proofs.** An auditor often needs to verify *one* saga
without seeing everyone else's. The records sit under a Merkle tree with
domain-separated leaves, so you can publish a single root and hand out a compact
inclusion proof for exactly one saga — every other run stays private.

```python
from agent_saga import audit_root, build_disclosure, verify_disclosure

root  = audit_root(all_records)                        # publish once
proof = build_disclosure(all_records, saga_id="checkout_402")
verify_disclosure(proof, root)                         # -> valid, and only 402 is revealed
```

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

By SagaOps. Implemented and tested (2007 tests; the base suite runs with only
`pytest`; optional extras add their own SDKs):

- **`AgentKit`** — the one-call agent-facing SDK: `safe_tool()` to wrap a tool,
  `transaction()` for a saga boundary, and machine-readable introspection with
  `guarantees()` (a versioned manifest of what's enforced, including an explicit
  `not_claimed` clause) and `status()` (live posture, fail-closed when a safety
  signal can't be read).
- **Rollback-safety certificates** (`agent-saga certify`): machine-checkable
  proof that every committed effect was accounted for; non-zero exit gates CI.
- **Selective-disclosure audit proofs**: a Merkle tree with domain-separated
  leaves lets you prove one saga under a published root without revealing others.
- **Predictive pre-execution**: speculatively run REVERSIBLE-only steps behind an
  HMAC lease bound to (intent, tool, expiry); stale/forged/cross-intent
  speculations can never be redeemed.
- **Passkey / hardware approvals**: an IRREVERSIBLE step can require a
  hardware-bound Ed25519 signature over a digest of the exact action.
- **Offline mesh sagas**: per-device local WALs merge with a commutative,
  idempotent, associative G-Set CRDT — any sync order converges.
- **Edge / async storage sink**: the engine is separable from disk (proven, not
  asserted), so the same guarantees run against any async store.
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

Available on PyPI: `pip install agent-saga`.

Known-pending, tracked openly (see [SECURITY.md](https://github.com/thomasjgeorge23/agent-saga/blob/main/SECURITY.md)): a shipped
distributed lock backend and async-native connectors. KMS/Vault key resolution
is an intended Enterprise-tier feature, deliberately absent from this BYOK core.

## Founder & Direct Contact

`agent-saga` and the SAGAOPS OS are created, architected, and maintained by **Thomas J George**.

**Tell us how it went — especially if it went badly.** A rollback library earns
trust from reports of the case it got wrong, not from testimonials. If a
compensation didn't fire, if `clean` said something you disagree with, if an
adapter broke against a newer SDK: that is the most useful thing you can send.

- 🐛 [**Open an issue**](https://github.com/thomasjgeorge23/agent-saga/issues/new) — bugs, adapter breakage, a `RollbackReport` that looks wrong. **Don't paste a production WAL**; ship a synthetic one that has the same shape and none of your data:
  ```python
  import json
  from agent_saga.synthetic import WALProfile, synthesize

  records = [json.loads(line) for line in open("saga.wal", encoding="utf-8") if line.strip()]
  profile = WALProfile.fit(records, redact=["email", "account_id"])
  shareable = synthesize(profile, sagas=50, seed=7)   # every record carries __synthetic__
  ```
  The profile keeps event order, tool mix and value *ranges*; it never keeps a
  value a person typed. Attach `shareable` — the failure reproduces, your
  customers don't travel with it.

- 💬 [**Post a review in Discussions**](https://github.com/thomasjgeorge23/agent-saga/discussions) — what you shipped it on, what it cost you, what you'd rip out. Public, so the next person reads it too.
- ✉️ **Email**: [thomasjgeorge23@gmail.com](mailto:thomasjgeorge23@gmail.com) — enterprise integration and anything you'd rather not post publicly.
- 🔒 **Security**: report privately per [SECURITY.md](https://github.com/thomasjgeorge23/agent-saga/blob/main/SECURITY.md), not in a public issue.
- 🐙 **GitHub**: [github.com/thomasjgeorge23](https://github.com/thomasjgeorge23)

The site's contact form saves to the browser it was typed in and, if you're
running `python site/server.py`, to a local `inquiries.json` readable with
`agent-saga inquiries`. It is **not** a hosted inbox and will say so rather than
show you a green tick — use the links above to reach a person.

## License

Apache-2.0. See [LICENSE](https://github.com/thomasjgeorge23/agent-saga/blob/main/LICENSE) and [NOTICE](https://github.com/thomasjgeorge23/agent-saga/blob/main/NOTICE).
Copyright 2026 Thomas J George (SAGAOPS).

Permissive on purpose. This library is `import`ed directly into the process that
moves your money, and a copyleft dependency on that path is something most legal
teams will not clear — so the thing designed to be trusted with production
traffic is licensed so that it can actually reach production. Apache-2.0 also
carries an **explicit patent grant** (section 3), which is the clause enterprise
review actually looks for.

- **Use it in a closed-source product.** No obligation to publish anything.
- **Run it as a service.** No network-use clause, no source-offer requirement.
- **Fork it.** Genuinely — just rename it, per [TRADEMARKS.md](https://github.com/thomasjgeorge23/agent-saga/blob/main/TRADEMARKS.md).

The code is open; the **name** is not (Apache-2.0 section 6 grants no trademark
rights). SagaOps' commercial products are separate hosted components — they are
not a license upgrade, and nothing in this repository is gated behind one.
