# Security Policy

`agent-saga` sits on the transaction path of autonomous agents: it records
side effects, holds compensation logic, and touches connectors that move money
and mutate systems of record. Security reports are taken seriously.

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Report privately through GitHub's **"Report a vulnerability"** button under the
repository's **Security** tab (Security Advisories). This opens a private
channel with the maintainers.

Please include:

- the affected version or commit,
- a description of the issue and its impact,
- a minimal reproduction if you have one.

We aim to acknowledge a report within 3 business days and to agree a disclosure
timeline with you before any public write-up.

## Scope worth special attention

Given what this library does, these areas are the highest-value to scrutinize:

- **Credential exposure in the WAL.** Credentials are meant to travel as
  *references* (`credential_ref`), never values; `assert_no_secrets` guards this
  at authoring time (including nested structures) and the debugger `scrub`s on
  read. A path that writes a real secret to the WAL is a vulnerability.
- **Recovery correctness.** Anything that causes `saga-recoveryd` to
  double-compensate (e.g. a refund issued twice) or to skip a real dangling
  effect.
- **Injection through agent-chosen inputs.** Table/column identifiers in the
  Postgres connector are LLM-chosen; the SQL identifier allowlist is a security
  boundary, not a nicety.
- **Snapshot store path handling.** Snapshot ids must never escape the store
  root.

## What is not yet in scope

Some hardening is known-pending and tracked openly rather than treated as a
vulnerability: WAL-at-rest encryption, authentication on the debugger UI, and a
distributed (non-filesystem) recovery lock. See the README roadmap. Reports that
these are *absent* are welcome as issues, but they are documented gaps, not
undisclosed ones.
