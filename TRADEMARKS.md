# Trademark policy

The **code** in this repository is Apache-2.0. The **name** is not.

"agent-saga", "SagaOps", and the SagaOps logos are trademarks of SagaOps.
Apache-2.0 grants copyright and patent rights; section 6 explicitly grants no
trademark rights. This document says what we will and will not object to, so you
do not have to guess.

## You may, without asking

- Say your product "works with agent-saga", "is built on agent-saga", or "is
  compatible with agent-saga" — accurate, factual statements of relationship.
- Redistribute unmodified releases under the name `agent-saga`.
- Use the name in articles, talks, benchmarks, comparisons, and course material,
  including critical ones.
- Publish plugins, connectors, and adapters named `agent-saga-<something>` or
  `<something>-for-agent-saga`, provided it is clear you are not us.

## You may not, without written permission

- Use "agent-saga" or "SagaOps" in the name of a **hosted or managed service**
  ("agent-saga cloud", "Managed agent-saga", "SagaOps Enterprise").
- Use the names in your company name, product name, domain name, or app-store
  listing.
- Use the logos in a way implying endorsement, affiliation, or certification.
- Ship a **modified** build under the unmodified name. Fork freely — that is what
  the license is for — but rename it, so that a user reporting a bug against
  "agent-saga" is reporting it against our code.

## Why this exists

This project sits on the transaction path of systems that move money. The value
of the name is that it identifies a specific, tested, auditable implementation.
If anyone could ship modified code under it, "we use agent-saga" would stop
meaning anything to the auditor a user is trying to satisfy — which is precisely
the thing this project is for.

Permissive code plus a protected name is a deliberate choice: it removes every
barrier to *using* and *forking* the software, while keeping the identity honest.

Requests and questions: legal@sagaops.dev
