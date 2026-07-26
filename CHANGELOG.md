# Changelog

All notable changes to `agent-saga` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow SemVer as of
0.4.0. One rule governs every entry: a line names a mechanism a user can go
run, or it does not appear.

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
