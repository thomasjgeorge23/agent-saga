# agent-saga — verified capability reference

Every row below was extracted from the source of **v0.5.1**, not written from
memory. Command help text is the argparse help string; connector handlers are
the names really passed to `@compensator`; adapter functions are the real public
callables in each module.

This matters because the last hand-written summary of this package invented an
API that does not exist (`crew_tool`, `llama_tool` — the real name is
`wrap_tool` in every adapter) and repeated two claims that had been retracted
for cause. `tests/test_capabilities_doc.py` now fails if this file drifts from
the code.

**Regenerate the raw data yourself:**

```bash
python -c "from agent_saga.cli import build_parser; import argparse; \
p=build_parser(); s=next(a for a in p._actions if isinstance(a,argparse._SubParsersAction)); \
print('\n'.join(sorted(s.choices)))"
```

---

## Install

**Zero required dependencies.** The core runs on the standard library alone;
everything below is optional.

```bash
pip install agent-saga                 # core
pip install "agent-saga[postgres]"     # PostgresWAL + approval store + SQLAlchemy
pip install "agent-saga[redis]"        # RedisWAL, distributed locks, limits, approvals
pip install "agent-saga[otel]"         # OpenTelemetry spans + OTLP export
pip install "agent-saga[encryption]"   # WAL encryption at rest
pip install "agent-saga[all]"          # everything
```

Framework extras: `langgraph`, `crewai`, `autogen`, `llamaindex`,
`openai-agents`, `salesforce`, `stripe`, `slack`. Dev extra: `dev`.

---

## CLI — all 26 commands

### Start here

| Command | What it does |
|---|---|
| `agent-saga demo` | watch a failure, a rollback, and a killed process get cleaned up |
| `agent-saga new <name>` | scaffold a runnable enterprise agent app |
| `agent-saga doctor` | audit production posture: the settings that silently lose effects |
| `agent-saga studio` | one-command local dev: dashboard + recovery daemon + WAL tail in one process |
| `agent-saga adopt` | scan an existing agent project and generate its saga wrappers |

`adopt` takes `--root`, `--out`, `--force`; it never decides a tool's
semantics, emitting `DECIDE` (a sentinel that raises) so the generated
module cannot even be imported until a human has classified each side
effect. `demo` takes `--no-color`, `--fast`, `--logs`. `new` takes `--directory/-d`,
`--force`. `doctor` takes `--replicas N` (promotes the process-local-WAL finding
from risk to blocker) and `--strict` (exit 1 on risks too, for CI).

### Recovery and incident response

| Command | What it does | Key options |
|---|---|---|
| `agent-saga recover` | run recovery daemon sweep over orphaned sagas | `--wal`, `--dry-run` |
| `agent-saga halt` | stop agents performing side effects, now | `--scope`, `--reason`, `--by`, `--ttl`, `--drain`, `--redis` |
| `agent-saga resume` | lift a halt | `--scope`, `--by` |
| `agent-saga status` | what is halted right now | `--file`, `--redis` |
| `agent-saga quarantine` | freeze one saga for investigation (not a rollback) | `--reason`, `--by`, `--release` |
| `agent-saga approvals` | list and answer pending human approvals | `--status`, `--approver`, `--note`, `--break-glass` |

### Audit and integrity

| Command | What it does | Key options |
|---|---|---|
| `agent-saga verify` | check the WAL hash chain for tampering | `--strict` |
| `agent-saga certify` | prove every committed effect is accounted for — non-zero exit on an orphaned/uncompensated effect, so it works as a CI gate | `--out`, `--check-handlers` |
| `agent-saga export` | export the WAL: a verified WORM bundle (`--out`) or flat CSV/JSON | `--format`, `--allow-broken` |
| `agent-saga audit-root` | print the Merkle commitment for a WAL — publish once, prove disclosures against it forever | `--wal` |
| `agent-saga prove` | emit a selective-disclosure proof for ONE saga, provable to an auditor without revealing any other | `--out`, `--note` |
| `agent-saga verify-proof` | verify a disclosure bundle against a published Merkle root | `--root` |
| `agent-saga reconcile` | ask the external systems whether the log is telling the truth | `--import`, `--verbose` |
| `agent-saga merge` | merge WAL segments from several devices into one deterministic log | `--wal`, `--out`, `--verify` |

### Inspection and development

| Command | What it does | Key options |
|---|---|---|
| `agent-saga ui` | launch the time-travel debugger over a WAL file | `--port`, `--host`, `--token`, `--auth` |
| `agent-saga replay` | walk a historical saga's steps, compensations, and failure point | `--execute` |
| `agent-saga graph` | draw a saga's forward path and rollback fork as Mermaid or Graphviz DOT | `--format`, `--saga`, `--all`, `-o` |
| `agent-saga refactor` | index a codebase and apply a scope-correct refactor as a transaction | `--symbol`, `--to`, `--root`, `--apply`, `--yes` |
| `agent-saga mcp` | run the saga proxy in front of an MCP server | `--policy`, `--observe`, `--emit-policy`, `--boundary` |
| `agent-saga cloud-server` | run self-hosted cloud control plane server | `--host`, `--port` |
| `agent-saga inquiries` | view physically stored user inquiries, reviews, and feedback | `--json` |

---

## Framework adapters — 11 modules

The public API is **`wrap_tool`** in every framework adapter. There is no
`crew_tool` or `llama_tool`.

| Module | Public API |
|---|---|
| `adapters.langgraph` | `wrap_tool`, `saga_run`, `build_runner` |
| `adapters.crewai` | `wrap_tool`, `saga_kickoff` |
| `adapters.autogen` | `wrap_tool`, `saga_run` |
| `adapters.llamaindex` | `wrap_tool`, `saga_run` |
| `adapters.openai_agents` | `wrap_tool`, `saga_run` |
| `adapters.framework_wrappers` | `wrap_crew`, `wrap_langgraph`, `wrap_autogen`, `wrap_swarm`, `wrap_llamaindex`, `wrap_method`, `is_saga_wrapped` |
| `adapters.temporal` | `saga_activity`, `SagaTemporalInterceptor` |
| `adapters.camunda` | `camunda_job_handler`, `SagaCamundaWorker` |
| `adapters.sqlalchemy` | `SQLAlchemyAdapter` |
| `adapters.supabase` | `SupabaseAdapter` |
| `adapters.base_db` | `BaseDBAdapter` |

Plus `frameworks.fastapi.saga_lifespan` for FastAPI startup/shutdown, WAL
initialization, and daemon drain.

**On `framework_wrappers`:** these open a real saga boundary around one method
and are idempotent; `is_saga_wrapped(obj, "method")` verifies it rather than
asking you to trust a log line. The boundary compensates steps *registered*
during the call — it cannot invent compensations for tools it never saw.

---

## Connectors — 6 modules, 13 registered handlers

Every handler below is registry-backed, so `saga-recoveryd` can run it from
another process after a crash.

| Connector | Registered handlers |
|---|---|
| `connectors.stripe` | `stripe.refund` |
| `connectors.postgres` | `postgres.restore_row`, `postgres.delete_inserted_row`, `postgres.reinsert_row` |
| `connectors.salesforce` | `salesforce.revert_object` |
| `connectors.github` | `github.close_issue`, `github.close_pull_request`, `github.revert_commit` |
| `connectors.messaging` | `messaging.delete_slack_message`, `messaging.delete_discord_message` |
| `connectors.cloud` | `cloud.terminate_instance`, `cloud.delete_s3_bucket`, `cloud.delete_k8s_pod` |

Notable behaviours, verified in source: Stripe uses idempotency keys so a
retried refund cannot double-refund; Postgres snapshots the columns it touches
and restores them without holding a lock across model thinking time, raising
`ConcurrentModification` if the row moved underneath; Salesforce guards its
revert with `LastModifiedDate` and raises `StaleObject` rather than clobbering a
concurrent edit.

---

## Core capabilities

| Capability | Module | Note |
|---|---|---|
| Saga boundary, LIFO compensation | `context.py`, `decorator.py` | `RollbackReport.clean` distinguishes a whole unwind from a partial one |
| Typed semantics | `semantics.py` | `REVERSIBLE` / `COMPENSABLE` / `IRREVERSIBLE`, author-declared, never inferred |
| Declarative inverses | `inverses.py` | `@inverse_of`, `delete_by`, `call_with`, `map_result` |
| Pre-flight gate, approvals | `gate.py`, `approvals.py` | M-of-N multi-sig; refusal before the effect |
| Write-ahead log | `wal/` | file, mmap, Postgres, Redis; hash-chained; `barrier()` durability fence |
| Crash recovery | `recovery.py` | expired leases (not PIDs); deterministic tokens prevent double-compensation |
| Context receipts | `context_broker.py` | HOT/WARM/COLD tiers; drifted summaries are evicted, never served |
| Synthetic WALs | `synthetic.py` | learn a log's shape, generate any volume of it; carries no real values, and every record is marked `__synthetic__` so it can never be mistaken for evidence |
| Training corpus from the log | `corpus.py` | labels every action by whether reality kept it; a step rolled back for a LATER failure is COLLATERAL, not a negative, so the dataset is not poisoned |
| Counterfactual replay | `counterfactual.py` | evaluate a cheaper model against recorded history with zero side effects; a divergence is reported UNKNOWABLE rather than guessed |
| Argument provenance | `provenance_gate.py` | refuses a side effect whose critical argument was model-invented; SOURCED is verified against the document, not accepted on the label |
| Grounded answers | `grounding.py` | `VERIFIED` / `UNCITED` / `BROKEN_CITATION` / `BROKEN_QUOTE` / `UNSUPPORTED` |
| Multi-model routing | `ir.py`, `router.py` | every decision and refusal names every candidate and reason |
| Verification-gated cascade | `cascade.py` | cheapest tier that passes a real check; escalates on evidence carrying the rejection reason; every tier's cost reported |
| Agent loop | `agent_loop.py` | deadlines, token budgets, stall detection; the goal is the transaction |
| Codemod pipeline | `codemod/` | index → plan → gate → apply, all four stages |
| Graph export | `graph.py` | rollback fork as Mermaid/DOT; a partial rollback can't draw as clean |
| Readiness audit | `readiness.py` | the postures that silently lose effects |
| Project scaffold | `scaffold.py` | a runnable app whose own tests pass |
| Cost/token budgets | `cost_gate.py` | raises `CostBudgetExceeded`; real enforcement |
| Blind audit commitments | `zkp.py` | keyed HMAC + Merkle inclusion proofs |
| Cross-agent coordination | `multi_agent_mesh.py` | distributed saga across agent processes |

---

## What this is not

Stated plainly, because two of these were claimed in a previous release and
retracted in v0.5.1 for cause:

- **`multi_agent_mesh` is not two-phase commit.** Each participant's step
  executes immediately — there is no vote and no durable prepared state — so an
  abort compensates effects that already happened. That is the saga trade-off.
  Coordinator state is also in-memory; give participants their own WAL for
  durability.
- **`zkp` is not zero-knowledge.** It is a keyed commitment scheme with Merkle
  inclusion proofs. It hides payloads from anyone without the key; it does not
  prove statements *about* a payload. The class is `BlindAuditCommitments`;
  `ZeroKnowledgeAuditProof` remains as a deprecated alias, and the key is
  required (releases up to 0.5.0 shipped a constant default, which made every
  commitment recoverable by anyone who installed the package).
- **`dashboard.py` has no authentication.** Safe on `127.0.0.1` only; use
  `agent-saga ui` for anything else.
- **Nothing here makes a model smarter.** The runtime makes whatever model you
  have budgeted, audited, repaired once on malformed output, stopped when it
  thrashes, and rolled back when it fails.
- **A saga has no isolation.** Each step commits as it goes, so intermediate
  state is briefly visible. `patterns/tentative.py` makes that legible rather
  than pretending otherwise.
