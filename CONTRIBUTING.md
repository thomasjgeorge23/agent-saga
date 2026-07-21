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

## Licensing of contributions

Inbound equals outbound: your contribution is licensed under **Apache-2.0**, the
same license the project ships under. Nothing more is asked of you.

**There is no CLA, and there will not be one.** A CLA exists so a maintainer can
relicense your code into a proprietary edition later. That can't happen here,
because SagaOps' commercial work is *separate hosted software*, not a closed
build of this repository — so there is no proprietary edition for your patch to
be pulled into, and no reason to ask you to sign away the right. If that ever
changed, it would require asking every contributor, in public.

Sign off your commits to certify you have the right to submit the work, under
the [Developer Certificate of Origin](https://developercertificate.org/):

```bash
git commit -s -m "your message"
```

Apache-2.0 section 5 already places inbound contributions under the project
license; the sign-off is the auditable record that you meant to.
