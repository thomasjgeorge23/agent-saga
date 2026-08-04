# Changelog

All notable changes to `agent-saga` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow SemVer as of
0.4.0. One rule governs every entry: a line names a mechanism a user can go
run, or it does not appear.

## [0.5.1] - 2026-07-29

**Correctness and security fixes for three features introduced in 0.5.0.** All
three were the same defect the `universal` engine was fixed for in 0.4.0: a
safety layer reporting protection it did not provide. If you are on 0.5.0 and
use any of them, upgrade.

### Fixed

- **`adapters.framework_wrappers` opened no transaction.** `wrap_crew`,
  `wrap_langgraph`, `wrap_autogen`, `wrap_swarm`, and `wrap_llamaindex` logged
  "inside agent-saga transaction boundary" and then called the original method
  unchanged — the `AgentKit` they created was never used. A wrapped agent had
  no boundary, no gate, and no rollback while appearing protected. The
  wrappers now run the call inside a real saga transaction (sync and async),
  are idempotent, raise instead of silently returning an unprotected object
  when no wrappable method exists, refuse clearly when a sync method is called
  inside a running event loop, and expose `is_saga_wrapped()` so the claim is
  checkable rather than merely logged.
- **`zkp` was neither zero-knowledge nor verifying.** Three separate breaks:
  the HMAC key defaulted to a constant compiled into the published package
  (so any installer could recover committed payloads by enumerating
  low-entropy fields like amounts and emails); `merkle_path` held the tree
  root instead of the sibling path; and verification asked whether the root
  appeared in that field, so a fabricated commitment — or one lifted from a
  different tree — verified true. Now: the key is required (minimum 16 bytes,
  no default), inclusion proofs carry real sibling paths with RFC 6962 domain
  separation, and `verify()` recomputes the root and compares it. Renamed to
  `BlindAuditCommitments` to describe what it is; `ZeroKnowledgeAuditProof`
  remains as a deprecated alias. **Breaking, deliberately:** the old default
  key and the tautological verification could not be preserved without
  preserving the vulnerability.
- **`SagaMeshCoordinator.rollback_mesh_saga` reported dirty unwinds as
  clean.** It logged "ROLLED BACK cleanly" and returned `state:
  "ROLLED_BACK"` even when compensations raised, and counted steps with no
  compensation at all as though they had been undone. The report now carries
  `clean`, `failed_steps`, and `orphaned_steps`, and the state becomes
  `ROLLED_BACK_PARTIAL` when either is non-empty — the `RollbackReport.clean`
  doctrine the rest of the engine is built on.

### Changed

- `multi_agent_mesh` no longer describes itself as two-phase commit. It
  executes each participant's step immediately, so there is no prepared state
  and no vote: it is a distributed saga, and the docstring now says so along
  with its two real limits (in-memory coordinator state, and compensations
  called with forward arguments rather than derived from the result).
- `dashboard` documents that it has no authentication and is safe only on
  `127.0.0.1`; `agent-saga ui` remains the auth-capable console.

## [2.0.1] - 2026-08-04 — PyPI Century-Grade Alignment

### Hardened & Synchronized
- **PyPI Default Release Alignment** — Released `v2.0.1` to align PyPI project homepage (`https://pypi.org/project/agent-saga/`) to the latest century-grade code release with 2,611 verified passing test suites.

## [Unreleased]

### Added

- **Semantic Kernel and Vertex AI adapters** — the two frameworks the adapter
  matrix was genuinely missing (13 modules now). Both follow the established
  pattern: the routing core is the shared `_common.build_runner`, so joining the
  active saga, passing arguments to the gate and LIFO rollback are proven by
  tests that need no SDK, while the SDK-specific packaging has an integration
  test that skips when the SDK is absent. Each docstring says which half is
  which rather than implying both are equally verified.
  `vertex_ai` additionally ships `dispatch_function_call()`, because Vertex does
  not execute tools for you and the natural handling — `registry[call.name]` —
  raises a bare `KeyError` deep in a response loop when the model hallucinates a
  tool. It raises `UnknownFunctionCall` naming what was asked for and what
  exists, or with `strict=False` returns a structured error to feed the model
  back its own mistake. `wrap_tool` preserves `__name__`, `__doc__` and the
  signature, so the schema Vertex generates by introspection is unchanged.
- **`docs/BENCHMARKS.md`** — published numbers with methodology. What is
  measured (the overhead the library adds, against a bare-await baseline) and
  what is not; the fast path (~17 us) and durable path (~7.9 ms) reported
  separately and never blended; group commit shown in both directions (31x
  throughput from 1 to 256 concurrent sagas, 9.6x latency); and the WAL measured
  against the device's own `fsync` floor so the marginal cost (0.97 ms, 27%) is
  the figure that travels between machines. States plainly what the numbers do
  not establish: scale beyond one node, your hardware, or correctness under
  failure — that last one being `verification.py`'s job.

- **OpenTelemetry GenAI semantic conventions** (`observability/genai.py`).
  agent-saga has always emitted good spans — under `saga.*` names it invented,
  which meant every observability backend received them and understood none of
  them. Spans now also carry `gen_ai.system`, `gen_ai.operation.name`,
  `gen_ai.request.model`, `gen_ai.usage.*`, `gen_ai.tool.name`,
  `gen_ai.response.finish_reasons` and `error.type`, and follow the convention's
  span naming (`chat {model}`, `execute_tool {name}`) — so Langfuse, Arize
  Phoenix, Datadog LLM Observability, Grafana and Honeycomb render them in the
  panels already built for them, with no configuration. Wired into both the
  router's model calls and `SagaContext.execute`'s tool calls.
  **Additive, never a rename:** `saga.semantics` and `saga.is_compensation` have
  no equivalent in the conventions and are what make a rollback legible, so both
  vocabularies are emitted and no existing dashboard breaks.
  **Message content is off by default:** prompts and tool arguments carry
  customer data and a tracing backend is usually a third party nobody classified
  as a data store, so capture requires
  `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true` or an explicit
  argument — and an explicit `False` always beats the environment.
  Unknown token counts are omitted rather than written as zero, and `error.type`
  records the exception class rather than a message that could carry an account
  number.

- **`agent_saga.verification`** — exhaustive bounded verification of the
  rollback state machine, answering the fair criticism that "tests check the
  cases somebody thought of". `verify_rollback_invariants(max_steps=6)`
  enumerates **every** failure interleaving up to N steps (which step's forward
  call raises x which subset of inverses then refuse x whether a committed step
  has no inverse) and executes each against the real `saga_scope` and
  `SagaContext` — not a model of them. 321 interleavings at N=6, seven
  invariants: LIFO attempt order, no successful double compensation, bounded
  retries, exactly one outcome bucket per step, `clean` never claimed over an
  incomplete rollback, halt strands only earlier steps and reports them
  UNRESOLVED, and an inverse-less committed step reported ORPHANED rather than
  dropped. The report **states its bound**: this is bounded model checking of
  the implementation, not a proof for unbounded N and not a claim about
  concurrency or partitions, which `test_chaos.py`, the `crash_worker`
  subprocesses and `test_mesh_fuzz.py` cover instead.

- **`agent_saga.risk`** — failure prediction mined from the log. The one
  statistical component in an otherwise deterministic architecture, and fenced
  in accordingly: it **advises**, it does not decide. `FailureModel.fit()`
  learns which call shapes had to be undone, reusing `corpus`'s labels so a step
  rolled back because a *later* step failed is not counted as a failure of its
  own — without that attribution the model learns "charging money is dangerous",
  which is true and useless. Reports **lift over the base rate** rather than a
  raw frequency, because "9 of 12 failed" is meaningless when three quarters of
  everything fails. Below its support threshold it produces no number and says
  plainly that silence is not a clean bill of health. Features are explainable
  by design (tool, argument shape, numeric decile) so an operator can act on the
  answer. Ships **no automatic blocking gate**: `require_review_above()` is an
  explicit opt-in that raises `REQUIRE_APPROVAL`, not `BLOCK`, because a
  resemblance warrants a human looking rather than a refusal.

- **`agent_saga.synthetic`** — learn the statistical shape of a real WAL and
  generate any volume of traffic from it. Solves two problems at once: a WAL
  cannot be shared (it holds amounts, addresses, customer ids), and recovery
  cannot be load-tested on fifty sagas. `WALProfile.fit()` retains only
  aggregates — tool frequencies, first-order transitions, saga lengths, outcome
  rates, numeric ranges — so **the profile itself is shareable**: fit it where
  the data is, ship the profile, synthesise anywhere. Output is structurally
  valid and hash-chained, so `verify`, `certify`, `build_corpus`,
  `wal_to_mermaid` and the recovery daemon accept it for the right reasons.
  Every record carries `__synthetic__: true` and every saga id is prefixed
  `synthetic-`, because an audit log indistinguishable from a real one is an
  instrument for fabricating evidence rather than a test fixture;
  `is_synthetic()` is strict about mixed logs for the same reason.
  `value_synthesizer=` is the seam where a CTGAN sampler drops in — the default
  keeps no dependency and never reproduces an observed value.
  Numeric fields preserve magnitude, not values: ranges are widened at fit time
  so a field observed with a single value cannot be reproduced exactly, and
  `fit(redact=[...])` drops fields that are identifiers rather than magnitudes.

- **`agent_saga.cascade`** — verification-gated cascade routing. The honest
  answer to "make a cheap model perform like an expensive one": you cannot, but
  you can stop trusting it and start checking it. `cascade(request,
  ladder=[local, mid, frontier], verify=...)` runs the cheapest host first and
  returns its answer **only if a check that can actually fail passed it** — a
  schema, a grounding receipt, `tools_must_exist()`, an entailment judge. On
  failure it re-asks the same host once with the precise rejection reason
  (cheap correction), then escalates carrying that reason upward. Nothing
  unverified is ever returned: exhaustion raises `CascadeExhausted` with the
  full trace, because returning the last answer anyway would make the cascade
  decorative. Escalation is evidence-driven rather than confidence-guessed —
  "does this tool exist?" has one right answer where "are you sure?" does not.
  Tokens from *rejected* tiers are counted too, since counting only the winner
  would flatter the result, and the whole ladder lands in the WAL as
  `CASCADE_RESOLVED`. Complements `router.complete()`, which deliberately
  refuses to fall *down* the ladder on a validation failure.
- **`agent-saga adopt`** — scan an existing agent project, detect its
  frameworks, find its tools across three definition styles (`@tool`
  decorators, `StructuredTool.from_function`, `BaseTool` subclasses), and
  generate the wrapping module. It never decides a tool's semantics: every
  entry is `semantics=DECIDE`, a sentinel that raises, so the generated module
  **cannot even be imported** until a human has classified each side effect.
  Name-based hints are offered and explicitly labelled as hints. Addresses the
  project's real weakness — onboarding, not capability.

- **`agent_saga.repair`** — surgical repair (edit-and-resume). A human can fix
  the step that failed and finish the transaction instead of unwinding it,
  which is what a malformed argument at step four of six actually calls for.
  Built so it cannot become a hole in the guarantee it sits inside: a session
  **refuses to resume unless every retained step has a registry-backed
  compensation**, because keeping steps whose inverse is an in-process closure
  would manufacture the orphan the engine exists to prevent; the resumed
  continuation runs in a saga whose stack **inherits** those steps, so a later
  failure unwinds the whole transaction rather than half of it; and `operator`
  and `reason` are mandatory with every action (`REPAIR_OPENED`,
  `REPAIR_RESUMED`, `REPAIR_ABANDONED`, amendments with their before-values)
  written to the same hash-chained log — an unaudited escape hatch would void
  the audit around it. Entry points are a crashed saga (no terminal record,
  effects still standing) or `agent-saga quarantine`.


- **`agent_saga.fleet`** — cross-framework orchestration with verifiable
  coverage. One `saga_scope` already spanned CrewAI/LangGraph/AutoGen tools
  (the boundary is a contextvar), but that mechanism had two silent failure
  modes. `SagaFleet` closes both. **Thread propagation:** measured, the saga
  contextvar survives `asyncio.to_thread` but *not* a raw `threading.Thread`
  or `ThreadPoolExecutor` — and frameworks dispatch tools through their own
  executors, at which point the ordinary runner performs the side effect with
  no WAL record and no compensation while still logging as saga-aware. The
  fleet re-attaches its active saga, and `bind_saga()` does the same for any
  callable. **Silent gaps:** a registered tool called outside a boundary now
  raises `BoundaryRequired` instead of running unprotected, and `coverage()`
  publishes a machine-readable manifest — which tools, from which framework,
  which have a recoverable inverse versus an in-process closure —
  with `assert_fully_covered()` for a startup or CI gate.
- `examples/cross_framework.py` — one saga across three frameworks, one
  rollback, proven by the WAL showing a single saga id. The LangChain side is
  a real `langchain_core` tool through the real adapter.

- **`agent-saga demo`** — three acts, five seconds, no network or
  configuration, showing the guarantee instead of describing it. Act I: an
  ordinary agent fails halfway and leaves a charge and a running server
  behind. Act II: the same calls inside a saga, same failure, world restored,
  with the `clean` label read from `RollbackReport` rather than hardcoded.
  Act III: the process is **killed** mid-transaction with `os._exit()`
  (skipping `finally`, `atexit`, and loop shutdown) and a **separate process**
  refunds the charge from the write-ahead log alone. Everything in it is the
  real engine — real WAL, real gate, real compensations, a real killed
  subprocess — and the world is a JSON file so damage outlives the process
  that caused it. `--logs` shows the engine's own trace, `--no-color` for CI.
  Output is ASCII-only and tested against a cp1252 console.

- **`agent_saga.inverses`** — declare what undoes a tool once, next to the
  function that does it, instead of writing a compensation factory at every
  call site. `@inverse_of(forward, maps={...})` pairs an inverse with its
  forward action; `AgentKit.safe_tool` then finds it when no `compensate=` is
  given. `delete_by()` covers the created-something-returned-its-id shape,
  `call_with()` covers inverses whose target is known before the forward call
  (and therefore stay valid on an `UNKNOWN` outcome), and `map_result()` is
  the general form. Addresses the most-cited friction in the library without
  trading anything away: semantics are still author-declared and never
  inferred, pairing an inverse that is not registered with `@compensator`
  raises at import (a closure cannot be run by `saga-recoveryd` after a
  crash), a forward result missing the field an inverse needs raises naming
  both what it wanted and what it saw, and an explicit `compensate=` always
  wins. Falling back to a declared inverse can only improve an outcome — the
  step would otherwise have been reported `ORPHANED`.

- **`agent_saga.codemod.index`** and **`agent_saga.codemod.plan`** — the three
  codemod stages `DESIGN_SPEC` promised and only stage 4 had delivered.
  `SymbolIndex.build()` produces a **scope-correct** symbol and reference
  graph: a `Name` resolves to a module-level symbol only when no enclosing
  function scope binds it, `global`/`nonlocal` are honoured, class bodies are
  not enclosing scopes for their methods, comprehensions and lambdas get their
  own scopes, and cross-module references resolve through the import graph
  (bare name, dotted attribute, and the name inside `from x import y`).
  Anything it cannot prove — star imports, `getattr`/`globals()` access — is
  recorded as `Unresolved` and widens the blast radius rather than being
  silently missed. `rename_symbol()` and `remove_unused_imports()` produce a
  `Plan`: exact character-span rewrites, a computed blast radius, a unified
  diff, and `requires_review` — which is `gate.py`'s doctrine applied to
  source code. Plans flow into the existing transaction via `plan_transform`,
  so a failed verification restores the tree.
- **`agent-saga refactor`** CLI — `rename` and `unused-imports`. Dry-run by
  default (the diff is the product); `--apply` writes through the codemod
  transaction, and a plan flagged for review additionally requires `--yes`.
- Renames splice identifier spans only, so comments, blank lines, odd spacing,
  and file encodings survive byte-for-byte. Validated against agent-saga's own
  106-module codebase: 86 unused-import edits across 50 files, every rewritten
  file compiling and the patched copy importing cleanly.

- **`agent_saga.scaffold`** and **`agent-saga new <name>`** — generates a
  runnable enterprise agent app: a FastAPI service whose `/readyz` probe fails
  closed so a misconfigured replica never joins the load balancer, tools that
  declare their semantics and register their inverses by name (so recovery
  works across processes), env-driven WAL backend selection, Dockerfile and
  compose file, an ops checklist, and a test proving the rollback actually
  runs. Refuses to overwrite edited files without `--force`. The generated
  project's own tests are executed by this package's suite, so "correct on the
  first run" is a tested claim.
- **`agent_saga.readiness`** and **`agent-saga doctor`** — audits production
  posture from live runtime state: a process-local WAL (a risk alone, a
  **blocker** once `--replicas` makes it arithmetic), `DROP_SILENT`
  backpressure and already-dropped records, in-process-only compensations,
  unchained logs, unbounded durability fences, missing encryption or
  telemetry. Exit 1 on blockers, `--strict` to fail on risks in CI. A clean
  report lists every check that ran, so it is evidence of work rather than of
  a checker that skipped everything.

- **`agent_saga.graph`** — graph export for saga execution and DAG plans.
  `wal_to_mermaid` / `wal_to_dot` draw the forward path *and the rollback
  fork*, rendering the three undo outcomes as visually distinct states:
  `compensated` (clean), `COMPENSATION FAILED - needs a human`, and
  `ORPHANED - no undo exists`. A partial rollback can never draw like a clean
  one — `RollbackReport.clean` doctrine, expressed in pixels.
  `dag_to_mermaid` / `dag_to_dot` render `DAGSaga` plans with live node
  status, and a dependency on an unregistered node is drawn as `MISSING`
  rather than silently dropped. Reconstruction is total (malformed, hostile,
  and truncated logs render rather than raise), user data never becomes
  diagram syntax, and output is deterministic so a diagram can be committed
  and diffed.
- **`agent-saga graph`** CLI: `--format mermaid|dot`, `--saga <id>`, `--all`,
  `-o <file>`. Defaults to the most recent saga in the log and says so when
  the log holds more; records carrying no saga id are kept and reported,
  because absence of an id is not evidence of belonging elsewhere.

### Changed

- README: added a **capability matrix** mapping every shipped capability to
  its module and install extra. Prompted by an external review that concluded
  eight shipped subsystems were missing — a discoverability failure, not a
  code one.

## [0.4.2] - 2026-07-26

### Added

- **`agent_saga.grounding`** — grounded answers: a hallucination cannot pose
  as a sourced fact. `ground(answer, broker)` classifies every claim in a
  model's answer against the broker's receipts: `VERIFIED` (citation resolves
  now, quotes appear in the sources), `UNCITED` (the model's own assertion,
  explicitly labeled), `BROKEN_CITATION` (never admitted / evicted, with the
  eviction reason / receipts no longer resolve), `BROKEN_QUOTE` (quoted text
  in none of the cited sources), and `UNSUPPORTED` (a caller-supplied
  entailment hook rejected the claim). `GroundedAnswer.fully_grounded` is
  strict — one unlabeled claim spoils it — and the verdict lands in the WAL
  as `ANSWER_GROUNDED`, completing the audit chain: what the model saw, why
  it acted, what its answer could and could not prove.
- `ContextBroker.receipts()` and `ContextBroker.loss_reason()` — public
  accessors the grounding layer resolves citations through.

### Changed

- Test hygiene: the version test asserts semver shape instead of a pinned
  number (cross-package lockstep is enforced separately), and a
  timing assertion no longer depends on Windows monotonic-clock granularity.

## [0.4.1] - 2026-07-26

### Changed

- Claim audit completed: `ai_engine` and `mission_critical` docstrings state
  mechanism instead of magnitude; README core-principles section reworked;
  the README identity updated to the accountable agent runtime (five layers,
  one rule: every layer can prove what it did) with the transaction wedge
  leading; `pyproject` description now names only shipped mechanisms.

## [0.4.0] - 2026-07-26

The v0.4 core: a provider-neutral agent runtime built on the existing saga
engine, plus an honesty fix in the universal layer. Everything below ships
with tests; the suite stands at 2,000+ passing.

### Added

- **`agent_saga.ir`** — provider-neutral IR (`ChatRequest`, `ChatResponse`,
  `ToolCall`, `Usage`), declared-not-measured `Capabilities` descriptors, and
  the `HostAdapter` protocol. `provider_extra` rides through losslessly in
  both directions.
- **`agent_saga.router`** — deterministic capability routing with truthful
  failure: `NoCapableHost`/`AllHostsFailed` name every candidate and every
  disqualifying reason. Per-attempt deadlines (`HostTimeout`), bounded
  same-host retries with backoff, breaker integration, per-host wall-clock
  timings, bounded validate-and-repair (`ValidationExhausted` carries the
  full error history), and an `LLM_ROUTED` WAL event per decision.
- **`agent_saga.context_broker`** — HOT/WARM/COLD context tiers where every
  WARM summary carries SHA-256 span receipts into COLD sources. Receipts are
  re-verified at every pack (one fetch + one hash per referenced document; a
  changed document falls back to per-slice checks); drifted summaries are
  evicted and named, never served. Deterministic packing, byte-stable prefix,
  bounded tiers with recorded displacement, `CONTEXT_PACKED` WAL events with
  the output hash, disk-backed `FileColdStore`.
- **`agent_saga.agent_loop`** — the transactional agent loop: JSON-over-text
  tool protocol (works on hosts without native tool calling), one bounded
  repair round for malformed actions, stall detection, wall-clock and token
  budgets, per-tool timeouts. **The goal is the transaction**: stalls,
  exhausted budgets, and failing tools raise inside the saga and unwind every
  side effect of the attempt. Each `AGENT_DECISION` WAL event carries the
  content hash of the exact packed context that produced it.
- **`agent_saga.codemod`** — codemod-as-transaction: in-memory shadow tree,
  parse-and-compile gating of every staged file, pre-write verification hooks,
  a single COMPENSABLE write step with snapshot-and-manifest durability
  ordering, and the registered `codemod.restore_files` handler so a recovery
  daemon can restore files after `kill -9`. Restores re-hash every snapshot
  and confine paths to the project root.
- `examples/small_model_big_docs.py` — offline end-to-end run: a 2048-token
  host over a 440k-char document with receipts, plus the rollback run.
- `py.typed` (PEP 561): the public API is now visible to mypy/pyright.
- OpenTelemetry spans: `agent.loop.step` and `llm.host.attempt`, parenting
  the existing per-tool saga spans.

### Fixed

- **`universal.patch_all()` no longer reports success it did not achieve.**
  It returns a truthful per-SDK report (`patched` / `not_installed` /
  `already_patched` / `failed: <reason>`), is reversible via `unpatch_all()`,
  and a failing record hook can no longer break the user's LLM call.
- **`universal` audit signatures are real.** `audit_hash` is now a canonical
  SHA-256 a third party can recompute; previously it used Python's
  per-process-randomised `hash()` while labelled sha256.
- **`enhance()` works where agents live.** It refuses clearly inside a running
  event loop and directs callers to the new `aenhance()`; previously it raised
  an opaque error from inside async code.
- All three regressions are pinned by `tests/test_universal_honesty.py`.

### Changed

- `ContextBroker.pack()` accepts `extra_prefix=` so callers (e.g. the agent
  loop) can extend the stable block without mutating a shared broker.
