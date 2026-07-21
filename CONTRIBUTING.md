# Contributing to agent-saga

Thanks for considering a contribution. This library guards the transaction path
of autonomous agents, so the bar is correctness first, then clarity, then speed.

## Development setup

```bash
git clone <repo> && cd <repo>
python -m pip install -e ".[dev]"
python -m pytest -q
```

The core suite runs with **only `pytest`** — no network, no live database, no
real credentials. Connector and adapter tests use in-process fakes; the handful
of real-SDK integration tests `importorskip` their framework and are skipped
when it is absent. A contribution should keep the base suite dependency-free.

## Principles this codebase holds to

- **"Undo" is typed.** Every effect is `REVERSIBLE`, `COMPENSABLE`, or
  `IRREVERSIBLE`. If you cannot say which, you do not yet understand the effect.
- **Fail closed, and say so.** When something cannot be undone, it is reported
  (`ORPHANED`, `NEEDS_HUMAN`), never silently dropped. Tests assert on what the
  system *refuses* to do as much as what it does.
- **Write-ahead, then act.** Durable intent is recorded before a side effect,
  not after.
- **Credentials are references, never values.** Anything that could put a secret
  in the WAL must go through a `*_ref` and `resolve_credential`.
- **Comments explain *why*, at the altitude of the surrounding code.** Skip
  narration of what the line plainly does.

## Adding a connector

A connector is the real product surface, so it carries the most responsibility:

1. Pick the correct semantics and justify it in the module docstring (a shared
   database row is `COMPENSABLE`, not `REVERSIBLE` — see `connectors/postgres.py`
   for why).
2. Register the compensation handler by **name** via `@compensator`, with
   JSON-serializable kwargs, so `saga-recoveryd` can run it after a crash.
3. Derive the inverse at runtime from the forward result, and guard against
   clobbering a concurrent external change.
4. Run `assert_no_secrets` on the compensation kwargs.
5. Test rollback, the `UNKNOWN` (timed-out) outcome, and the concurrency guard —
   all against a fake, no live system.

## Pull requests

- Keep the diff focused; one concern per PR.
- Add or update tests for the behavior you change. New behavior without a test
  that would fail without it will be asked for one.
- Match the surrounding style; do not reformat unrelated code.
- Note any effect on the durability/latency story in the description.

## Reporting security issues

Not here — see [SECURITY.md](SECURITY.md) for the private channel.
