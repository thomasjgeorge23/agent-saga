# Agent-Saga — Launch Blueprint & Public Copy

**Companion:** [DESIGN_SPEC.md](DESIGN_SPEC.md) · **Owner:** DevRel
**Prime directive:** every public claim maps to a shipped, tested behavior. This is
not a compliance tax on the marketing — it *is* the marketing. See §1.

---

## 1. Positioning: sell the wedge, not the aura

> **v0.4 update — identity vs. wedge.** As of 0.4.0 the product is broader than
> the wedge: five subsystems (transactional execution, receipted context,
> truthful routing, the budgeted loop, codemod-as-transaction) under one rule —
> *every layer can prove what it did*. The **identity** is therefore "the
> accountable agent runtime" (see README and docs/AGENT_RUNTIME.md). The
> **launch wedge** below stays the undo story, deliberately: it is the sharpest
> demo, the clearest pain, and the door through which the rest of the runtime
> gets discovered. Identity is what we are; the wedge is how we enter the room.

### What we sell

**Agent-Saga is the undo button for AI agents.** One sentence deeper: when an
agent fails halfway through a five-step task, the four side effects it already
caused are still real — agent-saga wraps every tool call in a transaction with a
runtime-derived inverse, a crash-safe write-ahead log, and a pre-flight gate that
refuses irreversible actions without a human on the hook.

This wedge is defensible against every neighbor in the space:

| Against | The one line |
|---|---|
| try/except + manual cleanup | Your except block doesn't survive a process crash. The WAL does — a separate recovery daemon finishes the rollback. |
| Temporal / workflow engines | Temporal makes you declare compensations when you write the code. An LLM picks its tools at runtime, and you can't refund a `charge_id` you haven't seen yet. Compensations here are factories: `(forward_result) -> Compensation`. |
| LangGraph checkpointing | A checkpoint restores your *agent's* state. It does not un-send the wire transfer. Agent-saga restores the *world's* state. |
| Guardrails / validation layers | Validators inspect text before it becomes action. We own what happens *after* — including the failure at step 4 of 5. |

### What we do not sell — and why that's the strategy, not a concession

We do **not** claim agent-saga makes any model smarter, eliminates hallucination,
or multiplies reasoning. Three reasons, in ascending order of importance:

1. The code itself disclaims it — `patch_all()`'s docstring: interception "does
   *not* make a model smarter."
2. The launch audience (HN, r/MachineLearning, staff engineers evaluating agent
   infra) treats unfalsifiable enhancement claims as a signal the *rest* of the
   library is also aspirational. One "10x reasoning" bullet poisons the credible
   90%.
3. We have something rarer than a capability claim: **a safety library that
   proves it cannot overstate its own coverage.** v0.3.x had exactly that bug
   class — `patch_all()` reported success while patching nothing, and the
   "sha256" audit hash was Python's per-process-randomised `hash()`. We fixed it
   and pinned it with a public test suite (`tests/test_universal_honesty.py`)
   whose module docstring says why: *"False assurance in a safety library is
   worse than no feature, because the user stops looking for the real
   protection."* That sentence is the brand. No competitor is telling this story
   because telling it requires having lived it.

**Positioning statement (internal):** For teams giving LLM agents write access to
real systems, agent-saga is the transactional safety layer that makes agent side
effects recoverable and auditable — unlike workflow engines that require
compensations declared at authoring time, it derives the inverse at runtime from
the forward call's actual result, and unlike every other safety layer, its
honesty about its own coverage is enforced by its test suite.

---

## 2. PyPI package copy

### Short description (`pyproject.toml` — replaces the current one)

Current text promises "empowering any LLM" and "total peace of mind" — both are
exactly the unfalsifiable-aura genre §1 rules out, and "total peace of mind" is a
red flag *to this specific audience*. Replace with:

> Transactional side effects for AI agents: saga rollback with runtime-derived
> compensations, crash-safe WAL recovery, pre-flight approval gates, and audit
> reports that cannot overstate what actually happened.

### Long description opener (top of README as rendered on PyPI)

> **The undo button for AI agents.**
>
> When an agent hallucinates halfway through a multi-step task, the side effects
> it already caused are still real. `agent-saga` wraps each tool call, records a
> runtime-derived inverse action, and unwinds the whole transaction — in-process
> on failure, or from a separate recovery daemon if the process itself dies.
>
> - **Every side effect is classified** — `REVERSIBLE`, `COMPENSABLE`, or
>   `IRREVERSIBLE` — and the engine refuses to start the third kind without a
>   human approval on record.
> - **Compensations are derived at runtime**, not declared at authoring time:
>   `(forward_result) -> Compensation`. You can't refund a charge_id you haven't
>   seen yet.
> - **Rollbacks are honest.** `RollbackReport.clean` distinguishes a 100% clean
>   unwind from a partial one that needs a human. Partial failure pages someone;
>   it never reports "done."
> - **Zero required dependencies.** The core is stdlib-only; Postgres/Redis WAL
>   backends, OTel, and framework adapters (LangGraph, CrewAI, AutoGen,
>   LlamaIndex, OpenAI Agents, MCP) are extras.
> - **It cannot overstate its own coverage.** Interception reports are per-SDK
>   and truthful (`patched` / `not_installed` / `failed: <reason>`), audit
>   signatures are reproducible SHA-256 a third party can recompute, and the
>   test suite enforces both. In a safety library, false assurance is worse than
>   no feature.

*(Keep the existing 30-second `AgentKit` code block immediately after — it's
strong. `kit.guarantees()` returning a machine-readable manifest of what is
actually enforced is the product thesis in one API call.)*

---

## 3. Landing page copy (sagaops.dev)

### Hero

> # Your agent will fail at step 4 of 5.
> ## What happens to steps 1 through 3?
>
> agent-saga is the transaction layer for AI agents with write access: saga
> rollback, crash-safe recovery, and a pre-flight gate that won't enter an
> irreversible action without a human on the hook.
>
> ```bash
> pip install agent-saga
> ```
> **[Quickstart]** **[Read the manual]** **[Run the chaos demo]**

### Three pillars (section below the fold)

**1 — Undo derived at runtime.**
Workflow engines hard-code the compensating step at authoring time. Your agent
picks tools at runtime, and the inverse depends on the result — the row ID, the
charge ID, the message timestamp. agent-saga compensations are factories over the
forward result. That's the difference between a workflow engine and an agent
transaction layer.

**2 — Survives the crash, not just the exception.**
Intent is written to a hash-chained WAL *before* the side effect fires. If the
process dies mid-transaction, a separate recovery daemon replays the log and
finishes the unwind. And if the daemon hits a record it can't read, it halts and
alerts — it never assumes "no work to do."

**3 — Honest by construction.**
Every rollback report distinguishes *clean* from *partial*. Every audit signature
is a reproducible SHA-256 anyone can recompute from the recorded fields. Every
interception report names exactly which SDKs are actually covered. And the test
suite makes overstating any of it a CI failure.

### Proof strip (under the pillars)

> `python examples/multi_domain.py` — 5 systems, 1 transaction, no network needed.
> `python examples/chaos_demo.py` — optimistic vs. transactional, side by side.
> Apache-2.0 · zero-dependency core · [benchmarks with methodology](bench/)

### The honesty section (own scroll block — this is the differentiator)

> ## We shipped the bug this library exists to prevent.
>
> In v0.3.x, `patch_all()` reported `{"openai": True}` while patching nothing,
> and our "sha256" audit hash was Python's per-process-randomised `hash()` — a
> signature nobody could ever verify. A safety layer reporting success it did
> not achieve is the exact failure mode we tell you to fear in your agents.
>
> So we fixed it the way we'd tell you to: the report format now *cannot express*
> untruthful success, signatures are canonical SHA-256 a third party can
> recompute, and `tests/test_universal_honesty.py` pins all of it in CI.
>
> If a safety vendor has never found this bug in their own product, ask them
> what their tests would say if they had it.

*(Run this block by the maintainer before publishing — it's disarming and it will
land, but it's a public admission and the call is theirs, not DevRel's.)*

---

## 4. Launch sequence

**Phase 0 — pre-flight (gate to everything else):**
1. Commit the pending `universal.py` fix + honesty suite; release as v0.3.4 with
   a plain-spoken changelog entry ("patch_all no longer reports success it did
   not achieve"). The Show HN story requires this to be *shipped*, not pending.
2. Claim audit (§5) on README/MANUAL/site — one pass, delete or evidence.
3. Publish `bench/` methodology (hardware, workload, flags) for any number we
   keep citing.

**Phase 1 — Show HN.**
Title: `Show HN: Agent-saga – transactional rollback for AI agent side effects`.
First comment (self-posted): the runtime-derived-compensation insight, the
`REVERSIBLE/COMPENSABLE/IRREVERSIBLE` table, the chaos demo command, and one
frank paragraph on the honesty bug and its test suite. That comment does more
work than the landing page — HN converts on maintainers who volunteer their own
sharp edges (global monkey-patching is fragile → so it's opt-in, reversible, and
reports `failed:` honestly).

**Phase 2 — comparison content (weeks 1–4).** One deep post per neighbor from the
§1 table, each anchored on a runnable example: *"A checkpoint restores your
agent's memory. It does not un-send the wire transfer."* against LangGraph
persistence; the compensation-factory example against Temporal's saga docs.
Comparisons are respectful and technically precise — the audience overlaps with
those tools' users, and we want "use both" adoption, not a flame war.

**Phase 3 — ecosystem surface (months 2–3).** Framework-adapter tutorials
(LangGraph, CrewAI, MCP proxy), a conference-talk abstract built on the chaos
demo, and the plugin conformance kits from DESIGN_SPEC §5 as the "build a WAL
backend in an afternoon" contributor funnel.

---

## 5. Claim audit — fix before any launch push

| Where | Current claim | Action |
|---|---|---|
| `pyproject.toml` description | "ultimate… empowering any LLM… total peace of mind" | Replace with §2 short description |
| `universal.py` module docstring | "Transforms any raw LLM response … into a next-level … transaction" | Reword to match what `execute_enhanced` does: preview, transactional tool execution, healing proposal, reproducible audit signature |
| `speculative.py` docstring | "sub-microsecond … arbitrary AI tool calls" | Keep only with a `bench/` reproduction path; otherwise state mechanism, not magnitude |
| `hallucination.py` docstring | "Prevents … hallucinated parameters" | "Pre-flight state verification that *rejects* tool calls whose parameters fail reality checks" — reject/verify language, never prevent/eliminate |
| README `beast`/HA figures (e.g. 67k ops/sec) | Bare numbers | Re-run per release, cite hardware + workload, link `bench/`; else delete |
| MANUAL.md (104 KB) | Unaudited superlatives | Same one-pass sweep; MANUAL is the depth asset — it must be the *most* sober document, because it's where serious evaluators end up |
| Anything "zero-hallucination", "apex-tier", "feature parity with frontier models" | — | Never appears. Contradicted by the code's own docstrings. |

**The test:** every sentence of public copy either (a) names a mechanism a reader
can go run, or (b) gets deleted. Compelling and truthful aren't in tension here —
the mechanisms are genuinely novel, and the honesty story is the only moat a
competitor can't copy by Friday.
