# UMIP — Universal Multi-agent Interoperability Protocol

**Version 1.0**

A specification for making one transaction span several agent frameworks, so
that a failure anywhere unwinds everything — not just the part that failed.

---

## 1. The problem

A real job crosses frameworks. A LangChain agent books the van; a CrewAI agent
charges the card; an MCP server files the permit. Each framework knows how to
call tools. **None of them knows how to undo another's work.**

So when the permit is refused, the van stays booked and the card stays charged.
Every framework has a retry story; none has a *rollback* story that reaches past
its own boundary. That gap is where the money is lost.

The gap is not technical depth — it is a missing agreement. Undo needs three
facts about a step, and no framework records them.

---

## 2. The protocol

A **participant** is any callable, from any framework, that declares:

| Field | Meaning |
|---|---|
| `name` | Stable identifier. A WAL record and a compensation refer to a step by this name, so it must not change across deploys. |
| `semantics` | `REVERSIBLE`, `COMPENSABLE`, or `IRREVERSIBLE` — what "undo" means for this step. |
| `compensate` | A factory that, **given the forward call's own result**, produces the inverse action. |

That is the entire protocol. Everything else — the pre-flight gate, the
write-ahead log, LIFO rollback, approvals, budget limits — is already
framework-agnostic. A participant that declares those three facts is
indistinguishable from a natively-wrapped step.

### 2.1 Why `compensate` takes the result

The undo for "charge the card" is "refund `ch_9`" — and `ch_9` did not exist
until the forward call returned. A compensation declared at authoring time can
only guess. UMIP therefore derives it at runtime from the forward result, which
is what makes it correct for the action the agent actually chose.

### 2.2 Conformance rules

Two rules are enforced at registration, not documented and hoped for:

1. **A `COMPENSABLE` participant MUST supply `compensate`.**
   A step that claims to be undoable and is not is the exact failure this
   protocol exists to prevent.
2. **An `IRREVERSIBLE` participant MUST NOT supply `compensate`.**
   If it can be undone it is `COMPENSABLE`. If it cannot, a compensation is a
   lie — and the pre-flight gate would rely on it when deciding whether a human
   must approve the step.

`REVERSIBLE` participants need no compensation.

Violations raise `UMIPConformanceError` at `register()` — before any saga runs.

### 2.3 Naming

Names must be unique within a registry and stable across deploys. Registering a
duplicate raises: a WAL written yesterday refers to a step by name, and a
recovery daemon started tomorrow must resolve the same name to the same undo.

---

## 3. Using it

```python
from agent_saga import saga_scope
from agent_saga.umip import UMIPRegistry, Participant
from agent_saga.semantics import ActionSemantics as S

reg = UMIPRegistry()
reg.register(Participant("van.book",    "langchain", S.COMPENSABLE, book_van,    undo_van))
reg.register(Participant("card.charge", "crewai",    S.COMPENSABLE, charge_card, undo_charge))
reg.register(Participant("permit.file", "mcp",       S.IRREVERSIBLE, file_permit))

async with saga_scope(name="job-42"):
    await reg.invoke("van.book", when="tuesday")
    await reg.invoke("card.charge", amount=8000)
    await reg.invoke("permit.file", zone="R4")     # refused
# -> card refunded, then van cancelled. LIFO, across frameworks.
```

A decorator form is available:

```python
@reg.participant("inventory.check", "local", S.REVERSIBLE)
def check_stock(sku): ...
```

### 3.1 Synchronous participants

Many frameworks' tools are blocking (CrewAI's `_run`, a plain Python function).
UMIP runs them on a worker thread, so one framework's blocking tool never stalls
the loop the other participants are running on. This is verified by a test that
asserts a concurrent task keeps ticking while a sync participant sleeps.

### 3.2 Outside a saga

`invoke()` outside a saga boundary simply calls the underlying callable. A
participant is not a second kind of function; adopting UMIP does not force
everything into a transaction.

---

## 4. Across processes

Frameworks are not the only boundary. When agent A calls agent B over HTTP,
`EntanglementPropagator` carries the saga identity in request headers
(`X-Saga-Correlation-Id`, `X-Saga-Entanglement-Id`), and B's middleware binds it
for the request. A→B→C therefore share one distributed saga identity, and the
same three-fact contract applies on every hop.

```python
propagator.install_fastapi(app)          # B extracts and binds
propagator.apply_httpx(client)           # A and B inject on outgoing calls
```

---

## 5. What UMIP does not do

Stated plainly, because the boundaries are what make the guarantee meaningful.

- **It does not make an irreversible action reversible.** Declaring
  `IRREVERSIBLE` is how you tell the gate to demand a human *before* the step,
  which is the only real protection. UMIP surfaces that choice; it cannot
  invent an inverse that does not exist.
- **It does not provide distributed consensus.** Rollback is LIFO best-effort
  with a durable log, not two-phase commit. A compensation that fails is
  recorded and escalated, not retried into correctness.
- **It does not sandbox participants.** A participant runs with the privileges
  of the process that registered it.
- **It does not translate framework semantics.** A LangChain tool and a CrewAI
  tool remain themselves; UMIP only agrees on how their effects are undone.

---

## 6. Interoperability surface

`registry.manifest()` emits a machine-readable description of everything that
can join a saga in this process — the handshake a peer needs:

```json
{
  "umip_version": "1.0",
  "frameworks": ["crewai", "langchain", "mcp"],
  "participants": [
    {"name": "card.charge", "framework": "crewai", "semantics": "COMPENSABLE",
     "compensating": true, "description": ""}
  ]
}
```

---

## 7. Relationship to the native adapters

`agent-saga` ships adapters for LangGraph, CrewAI, LlamaIndex, AutoGen,
OpenAI Agents, Temporal, Camunda, and MCP. UMIP does **not** replace them —
it is the same routing core (`adapters._common.build_runner`) exposed as an
explicit contract. A UMIP participant and a hand-wrapped LangChain tool take an
identical code path, so there is no second implementation to drift.

Use the native adapter when you want a drop-in that keeps a framework's own tool
type. Use UMIP when the saga spans frameworks, or when a callable has no adapter
at all.

---

## 8. Guarantees

For a saga whose participants all conform:

1. Every `COMPENSABLE` step that committed is compensated on failure, in reverse
   order, across every framework involved.
2. Every step — forward and compensating — is recorded in the write-ahead log
   before its effect, so a crash mid-rollback is recoverable by the daemon.
3. An `IRREVERSIBLE` step is gated *before* execution, not apologised for after.
4. A non-conforming participant cannot enter a saga: it is rejected at
   registration.

Points 1–3 are the engine's existing guarantees. UMIP's contribution is that
they now hold **across framework boundaries**, which is where they previously
stopped.
