# Changelog

All notable changes to `agent-saga` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow SemVer as of
0.4.0. One rule governs every entry: a line names a mechanism a user can go
run, or it does not appear.

## [Unreleased]

### Added

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
