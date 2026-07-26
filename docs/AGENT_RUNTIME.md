# The v0.4 Agent Runtime

Five subsystems that turn the saga engine into a full agent runtime. Each one
keeps the package's founding rule: **every claim is a mechanism you can run,
and every failure is named.** This page is the working reference; the module
docstrings carry the design arguments.

```
ContextBroker      what the model SEES     (budgeted, receipted, hashed)
Router + IR        who SERVES the call     (deterministic, truthfully refused)
AgentLoop          what the model DOES     (transactional, budgeted, audited)
codemod            edits to CODE           (shadow-verified, snapshot-restored)
```

## Quickstart: the whole runtime in one page

```python
from agent_saga import (
    ActionSemantics, AgentLoop, Compensation, ContextBroker, LoopTool,
    Router, RoutingPolicy, saga_scope,
)

# 1. A broker: what the model sees. Documents live in COLD at full fidelity;
#    summaries enter WARM only with receipts that resolve.
broker = ContextBroker(prefix="You are the on-call assistant.")
spans = broker.add_document("runbook", open("runbook.txt").read())
broker.admit_summary("restart procedure is in section 4", spans[:2])

# 2. A router: who serves. Adapters implement agent_saga.ir.HostAdapter.
router = Router([my_ollama_adapter, my_cloud_adapter],
                RoutingPolicy(prefer=("local-7b", "cloud")),
                attempt_timeout=60.0, attempts_per_host=2)

# 3. Tools: every side effect declares its semantics; compensations are
#    factories over the forward result (or pre-state, for UNKNOWN-safety).
tools = [LoopTool(
    name="restart_service", description="restart one service by name",
    parameters={"name": "string"}, fn=restart,
    semantics=ActionSemantics.COMPENSABLE,
    compensate=lambda r: Compensation(fn=start, kwargs={"name": r["name"]},
                                      description="start it back up"))]

# 4. The loop, inside a saga: the goal IS the transaction.
loop = AgentLoop(router, broker, tools,
                 max_steps=8, budget_tokens=2000,
                 deadline_s=120.0, max_total_tokens=50_000)
async with saga_scope(wal=wal) as ctx:
    result = await loop.run("restart whatever is failing", ctx)
```

If the run completes, `result` carries the answer, the steps, tokens used, and
elapsed time — and the side effects stand. If it stalls, exhausts a budget, or
a tool fails, the raise unwinds every side effect through the saga, and the
WAL shows exactly what happened.

## The audit chain

Three WAL event families make an agent run reconstructable from the log alone:

| Event | Written by | Answers |
|---|---|---|
| `CONTEXT_PACKED` | broker | *What exactly did the model see?* (content hash + included/excluded/evicted with reasons) |
| `LLM_ROUTED` | router | *Why this host — and what did every other host get rejected for?* (full rejection map, per-host timings) |
| `AGENT_DECISION` | loop | *Why did the agent do that?* (`context_hash` matches a `CONTEXT_PACKED`, plus action, tokens, elapsed) |

These sit alongside the engine's own `STEP_INTENT` / `STEP_COMMITTED` /
`ROLLBACK_START` / `COMPENSATED` records, all on one hash-chained log.

## Honest boundaries (read before quoting any of this in a pitch)

- **Nothing here makes a model smarter.** The broker lets a small-context
  model *work over* corpora far beyond its window with verifiable compression;
  its reasoning is unchanged. Routing puts requests on capable hosts; the
  answer is whatever that host produces.
- **Capabilities are declared, not measured.** A wrong declaration misroutes —
  which is why every decision is WAL-recorded and diagnosable.
- **Token budgeting uses provider-reported usage when present, a chars/4
  estimate when not** — and each `AGENT_DECISION` records which one it was.
- **The codemod engine does not do lossless AST unparsing**, and it does not
  provide cross-file atomicity: per-file writes are atomic, and the durable
  snapshot manifest + `codemod.restore_files` handler is the cross-file
  guarantee (in-process or from the recovery daemon after `kill -9`).
- **Partial progress is not kept.** Today's loop is all-or-nothing by design;
  checkpoint-and-resume of a half-finished goal is future work and will be a
  policy you opt into, never a silent default.

## Hardening knobs

| Knob | Where | Default | Guards against |
|---|---|---|---|
| `attempt_timeout` | Router | 120s | a hung provider freezing the agent |
| `attempts_per_host` / `backoff_base` | Router | 1 / 0.25s | transient blips causing needless failover |
| `max_repair_rounds` | Router.complete | 1 | infinite re-prompting on invalid output |
| `deadline_s` | AgentLoop | off | runaway wall-clock |
| `max_total_tokens` | AgentLoop | off | runaway spend |
| `max_steps` / `max_repeated_calls` | AgentLoop | 8 / 3 | infinite loops and thrashing |
| `timeout` | LoopTool | saga default | one hanging tool |
| `max_hot_entries` / `max_warm_entries` | ContextBroker | 64 / off | unbounded memory in long-running agents |

Operational introspection: `router.hosts()` (declared capabilities in candidate
order), `broker.stats()` (tier sizes, evictions, displacements), plus the
existing `AgentKit.guarantees()` / `.status()`.

## Test map

`tests/test_router.py`, `tests/test_context_broker.py`,
`tests/test_agent_loop.py`, `tests/test_ast_transaction.py`,
`tests/test_production_hardening.py`, `tests/test_universal_honesty.py` —
each file opens with the numbered claims it proves. If a behavior on this page
has no test, that is a bug in this page.
