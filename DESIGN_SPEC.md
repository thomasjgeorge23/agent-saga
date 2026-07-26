# Agent-Saga — Software Design Specification

**Status:** Draft for v0.4 planning · **Audience:** contributors and enterprise evaluators
**Companion:** [LAUNCH_BLUEPRINT.md](LAUNCH_BLUEPRINT.md) (positioning and public copy)

---

## 0. Ground rules for this spec

Two rules govern every module below. They are not style preferences; they are the
lesson of the project's own history.

1. **Every capability claim maps to a test.** The v0.3.x `universal` engine shipped
   three defects of the same family — a safety layer reporting success it did not
   achieve (`patch_all()` returning `True` while patching nothing; an `audit_hash`
   labelled sha256 but built on Python's per-process-randomised `hash()`;
   `enhance()` raising inside the very event loops agents run in). All three are
   fixed and pinned by `tests/test_universal_honesty.py` (11 tests). The spec
   therefore defines each module by its **observable contract**, and the contract
   ships with conformance tests or it does not ship.

2. **Orchestration multiplies reliability, not intelligence.** Agent-Saga does not
   make a host model smarter — `patch_all()`'s own docstring says so. What a
   middleware layer *can* do, measurably: raise task-completion rates through
   verification loops and retrieval discipline, cut token cost through context
   tiering, and make failure recoverable through transactions. Improvements are
   stated as benchmarkable deltas (see `bench/`), never as intrinsic model gains.

### Existing foundation (what this spec builds on, not what it invents)

| Layer | Modules already in tree |
|---|---|
| Transaction core | `context.py` (saga scope, LIFO compensation), `semantics.py`, `gate.py`, `recovery.py` |
| Durability | `wal/` (file, postgres, redis, mmap), `snapshot.py`, `durable_memory.py`, `integrity.py` |
| Reliability | `retry.py`, `breaker.py`, `idempotency.py`, `locks.py`, `limits.py`, `ha.py` |
| Governance | `approvals.py`, `vault.py`, `encryption.py`, `tenant.py`, `provenance.py` |
| Integration | `universal.py`, `adapters/` (LangGraph, CrewAI, AutoGen, LlamaIndex, OpenAI Agents), `mcp/`, `connectors/` |
| Observability | `observability/` (OTel), `ledger.py`, `feedback.py` |

---

## 1. Unified Reasoner & Context Compression Engine

### 1.1 Problem

Long-running agents drown in their own transcripts. Re-feeding raw history is the
dominant token cost, the dominant latency source, and — past the model's effective
attention span — a net *accuracy loss*. The fix is not a bigger window; it is a
broker that decides, per call, what the model actually needs to see.

### 1.2 Architecture: the `ContextBroker`

A new module `agent_saga/context_broker.py` sitting between the agent loop and the
provider adapter. Three tiers, strict provenance:

```
┌─ HOT ────────────────┐  verbatim: system prompt, current task frame,
│ token-budgeted       │  last N tool results, active saga state
├─ WARM ───────────────┤  structured summaries, each carrying span refs
│ summaries + spans    │  (wal_seq, byte offsets) back to the record it compresses
├─ COLD ───────────────┤  full fidelity: WAL entries, durable_memory store,
│ WAL + durable_memory │  document chunks — retrieved on demand, never guessed
└──────────────────────┘
```

**Core rule — compression with a receipt.** A WARM summary is only admissible if
it carries resolvable span references into COLD storage. When the model asserts a
fact drawn from a summary and a downstream gate needs to verify it, the broker
re-hydrates the original span. This turns "context compression" from silent lossy
truncation into an auditable cache. It reuses the WAL's canonical-JSON hashing
(the same encoding `universal._audit_signature` uses) so a summary that drifted
from its source is detectable.

**Budgeter.** Deterministic packing per call: fixed system prefix first (byte-stable
across calls to maximise provider prompt-cache hits), then task frame, then WARM
summaries by recency-weighted relevance, then HOT tail. The prefix-stability rule
alone is a measurable latency/cost win on cached providers and costs nothing.

**Reasoning patterns** (composition of existing pieces, not new magic):

- *Plan → gate → execute → verify.* Plans render through `preview.py`
  (`PreviewSaga.build_plan`), gates through `gate.py`, execution inside
  `AgentKit.transaction()`, verification through `schemas.py` + `hallucination.py`
  reality anchors (pre-flight state checks on tool parameters).
- *Decomposition* via `dag.py` (`DAGSaga`) for tasks with independent branches —
  parallelism comes from the DAG's structure, and every branch stays inside the
  same saga boundary so a failure anywhere unwinds everywhere
  (`propagation.py` correlation headers extend this across processes).
- *Bounded self-correction* via `healing.py`: a failed step gets one structured
  repair proposal (`try_heal_with_proposal`), which is surfaced in
  `EnhancedAIResponse.self_healed_proposal` — never silently retried forever.

### 1.3 Contract & tests

- `pack(budget) -> PackedContext` is deterministic for identical broker state
  (property test: same state, same bytes).
- Every WARM entry's spans resolve; a summary whose source hash no longer matches
  is evicted and re-summarised, never served (test: tamper with COLD, assert
  eviction).
- Accuracy claims come from `bench/`: long-document QA and multi-step task suites
  run with broker on/off, reporting completion-rate delta and tokens/task. **No
  number appears in public copy that this harness did not produce.**

---

## 2. Static Analysis & Codebase Maintenance Module

### 2.1 Problem and stance

Automated refactoring across a multi-file repo is a *side-effect-heavy, partially
reversible operation* — exactly the shape of problem the saga core exists for.
The design insight: **a codemod is a transaction.** Nothing here promises
"autonomous repair of architectural flaws"; it promises mechanical transforms with
verification gates and a clean rollback story, which is what developers will
actually trust with write access.

### 2.2 Architecture: `agent_saga/codemod/`

New subpackage; note `propagation.py` is *distributed saga propagation* and is
unrelated — the codemod engine gets its own namespace.

**Pipeline (four stages, each a saga step):**

1. **Index.** Parse the repo into per-file ASTs — Python via `libcst` (lossless,
   comment-preserving; stdlib `ast` is insufficient for rewriting), other
   languages via `tree-sitter` grammars behind a common `LanguageFrontend`
   protocol. Build the symbol table and two graphs: the *import graph* (module →
   module) and the *reference graph* (definition → use sites). Cache keyed by
   content hash; incremental re-index on file change.
2. **Plan.** A transform (rename, signature change, dead-code removal, lint-class
   fix) is expressed as a pure function `Plan(graph) -> [Rewrite]`, where each
   `Rewrite` is (file, span, replacement, reason). The plan computes its **blast
   radius** — the transitive closure of affected definitions over the reference
   graph. The plan is data: printable, diffable, gateable.
3. **Gate.** Blast radius above a configured threshold (files touched, public API
   symbols changed) routes through `gate.py` → `approvals.py` for human sign-off,
   exactly like an uncompensable tool call. Small mechanical fixes flow through.
4. **Apply + verify, transactionally.** Before writes: file snapshots via
   `snapshot.py` (the `.agent_saga_snapshots/` machinery), semantics `REVERSIBLE`.
   Apply rewrites; then run the verification ladder — reparse (syntax), type
   check (`mypy`/`pyright` if configured), targeted tests selected by the
   reference graph. Any rung fails ⇒ the saga compensates and the tree is
   byte-identical to before (test: induced failure, assert restoration).

**LLM involvement is bounded on purpose.** The model proposes *which* transform
and *why* (it is good at intent); the AST engine computes *where* and *what*
(it must be exact). Free-text patches from the model are never applied directly —
they are parsed into `Rewrite`s or rejected. This is the same principle as
runtime-derived compensations: the trustworthy artifact is derived mechanically
from ground truth, not asserted by the model.

### 2.3 Contract & tests

- Round-trip: index → no-op plan → apply produces byte-identical files.
- Rename across N files updates every reference or aborts — no partial renames
  (property test over generated package trees).
- Verification-ladder failure restores the tree exactly (reuses
  `tests/crash_worker.py` patterns for kill-mid-apply crash safety).

---

## 3. Universal API Middleware & Adapter Layer

### 3.1 What exists and the contract it must keep

`universal.py` (as of the current working tree) is the reference implementation
of the layer's ethic, and it is worth stating as an invariant because v0.3.x
violated it:

> **The Truthful Report invariant.** Any operation that claims coverage returns a
> per-target report whose only values are `"patched"`, `"not_installed"`,
> `"already_patched"`, or `"failed: <reason>"` — and `"patched"` is only ever
> reported when the callable was really wrapped. Interception is reversible
> (`unpatch_all()`), transparent (responses pass through unaltered), and
> self-protective (a failing `record` hook must never break the user's LLM call).
> Enforced by `tests/test_universal_honesty.py`.

### 3.2 Design: normalize, describe, route, parse

**Intermediate representation.** `agent_saga/ir.py`: provider-neutral
`ChatRequest` / `ChatResponse` / `ToolCall` dataclasses with lossless
`provider_extra` passthrough for fields the IR does not model. Adapters translate
IR ↔ native SDK types; the saga core, gates, broker, and ledger speak only IR.

**Capability descriptors.** Each adapter publishes a static
`Capabilities(context_tokens, supports_tools, supports_json_mode, supports_streaming,
cost_class)` record. Descriptors are *declared* facts about the provider, not
measurements — the routing layer treats them as hints, and every routing decision
is written to the ledger so a wrong hint is diagnosable.

**Routing policy.** `route(request, policy) -> adapter`, where policy is ordered
rules over descriptors ("needs tools + 100k context → provider A; else cheapest").
Fallback chains reuse `breaker.py` (circuit-break a failing provider) and
`retry.py` (adaptive backoff). Routing is deterministic given (request, policy,
breaker state) — replayable from the WAL.

**Attachment modes,** ordered by preference:

1. *Explicit wrap* — `AgentKit.safe_tool` / IR adapters. Default in docs.
2. *Framework adapter* — the existing `adapters/` for LangGraph, CrewAI, AutoGen,
   LlamaIndex, OpenAI Agents; plus the `mcp/` proxy for MCP tool servers.
3. *SDK interception* — `patch_all()`. Documented as global, version-fragile
   monkey-patching, opt-in, reversible. The pinned SDK method paths in
   `_PATCH_TARGETS` are validated in CI against the versions we claim to support;
   a mismatch reports `failed:` rather than silently observing nothing.

**Output parsing.** Schema-first: expected structure declared via `schemas.py`;
parse → validate → at most **one** bounded repair round-trip (re-prompt with the
validation error) → typed result or a typed `ParseFailure`. Never an exception
from three frames inside an adapter, and never a silently-coerced value.

---

## 4. Multi-Tenant Enterprise Pipeline

### 4.1 Stance

This is the load-bearing layer, and most of it exists. The design work for v0.4
is *composition and posture*, not new machinery: one documented data path, one
fail-closed rule, per-tenant everything.

### 4.2 The execution path (normative)

```
submit(task, tenant)
  → tenant scope           tenant.py     contextvars namespace: WAL, limits,
                                         approvals, snapshots all tenant-keyed
  → admission              limits.py     rate/spend caps; idempotency.py dedupes
                                         retried submissions (exactly-once intent)
  → pre-flight gate        gate.py       semantics declared per tool; IRREVERSIBLE
                                         requires approval (approvals.py, multi-sig)
  → saga execution         context.py    WAL-first ordering: intent is durable
                           wal/*         before any side effect fires
  → failure handling       healing.py    one bounded repair proposal, else
                           context.py    LIFO compensation; RollbackReport.clean
                                         distinguishes clean from partial — partial
                                         pages a human, never silently "done"
  → crash recovery         recovery.py   separate daemon replays the WAL; an
                                         unreadable record halts and alerts
                                         (never "no work to do")
  → audit                  ledger.py     hash-chained; vault.py WORM retention;
                           provenance.py reproducible sha256 signatures
```

**Fail-closed posture rule (normative):** if any safety signal cannot be read —
WAL unwritable, approval store unreachable, limit store down — the pipeline
refuses new *side-effecting* steps for that tenant and surfaces the posture via
`AgentKit.status()`. Read-only work may proceed. This rule is testable and is the
single sentence that defines "enterprise-grade" for this project.

**Tenant isolation contract:** no code path may reach a WAL, limit store,
approval store, or snapshot store except through the tenant-scoped handle.
Enforced by a conformance test that runs two tenants concurrently through
crash-and-recover and asserts zero cross-tenant reads (extends
`tests/test_scaling.py` / chaos-worker patterns).

### 4.3 Throughput

Parallelism comes from `DAGSaga` fan-out within a transaction and horizontal
workers over shared WAL backends (postgres/redis), coordinated by `locks.py`
heartbeat leases — a worker that dies loses its lease and its work is reclaimed.
Numbers come from `bench/` with published methodology (see LAUNCH_BLUEPRINT §5);
existing figures (e.g. mmap-WAL latencies) are re-validated per release and
stated with hardware and workload, or not stated at all.

---

## 5. Long-Term Extensibility & Modular Blueprint

### 5.1 The honest problem

`agent_saga/` currently has ~65 top-level modules. That breadth was fine for
exploration; it is a liability for a decade-scale framework because every module
name in the public namespace becomes an implicit compatibility promise. The
blueprint is therefore mostly *subtraction*.

### 5.2 Three-ring structure

- **Ring 0 — `agent_saga.core` (stable, semver-guaranteed):** saga scope,
  `Compensation`, `ActionSemantics`, `AgentKit`, gate, WAL protocol, recovery,
  tenant scope, IR types. Deprecations announced one minor version ahead, removed
  no sooner than the next major; every deprecation ships a migration note and a
  `DeprecationWarning`.
- **Ring 1 — plugins (versioned separately, conformance-tested):** WAL backends,
  provider adapters, framework adapters, connectors, codemod language frontends.
  Each family gets a `Protocol` + a **conformance kit** — `testing.py` and
  `pytest_plugin.py` grow per-family suites (e.g. `WALConformance`: ordering,
  crash-mid-write, tamper detection) so a third-party backend proves itself with
  one pytest run. Discovery via Python entry points
  (`[project.entry-points."agent_saga.wal"]` etc.) — no registry service, no magic.
- **Ring 2 — `agent_saga.experimental`:** everything exploratory (speculative
  shadow engine, predictive staging, mesh, entanglement, beast coordinator).
  Explicitly exempt from stability guarantees; things graduate to Ring 0/1 by
  meeting the bar (conformance suite + two releases without interface change),
  or they eventually get folded in or retired.

### 5.3 Self-optimising loops, defined narrowly

"Self-optimising" means: `feedback.py` + the ledger accumulate per-tool failure
rates, repair success rates, and route outcomes; these tune *parameters within
declared bounds* — retry curves, breaker thresholds, routing weights, summary
compression ratios. Bounds are configuration; changes are logged; nothing
self-modifies code or policy. A feedback loop that can widen its own authority is
a failure mode, not a feature.

### 5.4 Forward compatibility

- WAL record format carries a version byte today; the reader keeps decoding every
  version ever shipped (append-only format evolution, `integrity.py` verifies
  chains across versions).
- The IR is additive-only within a major version; unknown fields ride
  `provider_extra` untouched.
- Model/provider churn is absorbed entirely in Ring 1 adapters — a new provider
  is a new plugin, never a core change. That, not prediction, is what a ten-year
  horizon realistically buys.

---

## 6. Rollout

| Phase | Deliverable | Gate to ship |
|---|---|---|
| v0.4.0 | Ring structure + `ContextBroker` MVP (tiering, budgeter, span provenance) | conformance kits green; broker property tests |
| v0.4.x | IR + routing policy; capability descriptors on all adapters | routing replay test from WAL |
| v0.5.0 | `codemod/` Python frontend (rename, signature change, dead-code) | round-trip + crash-restore tests |
| v0.5.x | Codemod tree-sitter frontends; broker benchmarks published | `bench/` methodology doc live |
| v0.6.0 | Fail-closed posture rule enforced pipeline-wide; tenant conformance suite | two-tenant chaos test green |
