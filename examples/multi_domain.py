"""multi_domain.py -- one agent, five systems, one transaction.

Payments are the loudest example of an uncompensable mistake, not the only one.
An agent that provisions a cluster, writes vectors to an index, opens a ticket
and posts to a channel has made four changes to four systems, and a failure at
step five leaves all four standing.

This is a single realistic workflow -- "onboard a new customer's search
environment" -- crossing every domain the engine is meant to cover:

    1. Cloud & Infrastructure     Terraform / AWS      provision an EC2 worker
    2. Data & AI Infrastructure   Pinecone / S3        create a vector namespace
    3. Enterprise SaaS            Jira                 open the onboarding ticket
    4. Developer Workflow         GitHub               open a config PR
    5. Messaging & Productivity   Slack                announce it in-channel

Each step is classified honestly:

    REVERSIBLE    the change is invisible to anyone else and restores exactly
    COMPENSABLE   the inverse is a new, visible action (terminate, delete, close)
    IRREVERSIBLE  no automated undo exists at all

The last step is the interesting one. A Slack post is COMPENSABLE (you can
delete the message) but a *notification already delivered to a phone* is not --
so the demo shows the pre-flight gate refusing an IRREVERSIBLE step rather than
cleaning up after it.

Nothing here touches a real network. `World` is an in-memory stand-in whose
state is printed before and after.

    python examples/multi_domain.py
    python examples/multi_domain.py --fail-at 4     # break a different step
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_saga import (  # noqa: E402
    ActionSemantics,
    Compensation,
    PreFlightGate,
    SagaAborted,
    compensator,
    saga_scope,
)

R = ActionSemantics.REVERSIBLE
C = ActionSemantics.COMPENSABLE
I = ActionSemantics.IRREVERSIBLE


# ===========================================================================
# The world: five systems an agent can leave half-mutated
# ===========================================================================

@dataclass
class World:
    instances: dict = field(default_factory=dict)      # AWS / Terraform
    namespaces: dict = field(default_factory=dict)     # Pinecone / Qdrant
    buckets: dict = field(default_factory=dict)        # S3
    tickets: dict = field(default_factory=dict)        # Jira
    pulls: dict = field(default_factory=dict)          # GitHub
    messages: list = field(default_factory=list)       # Slack
    notified: list = field(default_factory=list)       # Twilio / SendGrid

    def summary(self) -> list[tuple[str, int, str]]:
        return [
            ("EC2 instances", len(self.instances), "aws"),
            ("Vector namespaces", len(self.namespaces), "pinecone"),
            ("S3 objects", len(self.buckets), "s3"),
            ("Jira tickets", len(self.tickets), "jira"),
            ("GitHub PRs", len(self.pulls), "github"),
            ("Slack messages", len(self.messages), "slack"),
            ("SMS delivered", len(self.notified), "twilio"),
        ]

    @property
    def is_clean(self) -> bool:
        return all(count == 0 for _, count, _ in self.summary())


WORLD = World()


# ===========================================================================
# Compensations, registered by name so saga-recoveryd can replay them after a
# crash. A closure cannot cross a process boundary; a registry name can.
# ===========================================================================

@compensator("demo.aws.terminate_instance")
def terminate_instance(instance_id: str, idempotency_key: str = "") -> dict:
    # Idempotent: a compensation may be retried, or replayed by the daemon.
    WORLD.instances.pop(instance_id, None)
    return {"instance_id": instance_id, "state": "terminated"}


@compensator("demo.pinecone.delete_namespace")
def delete_namespace(index: str, namespace: str, idempotency_key: str = "") -> dict:
    WORLD.namespaces.pop(f"{index}/{namespace}", None)
    return {"namespace": namespace, "state": "deleted"}


@compensator("demo.jira.close_issue")
def close_issue(issue_key: str, idempotency_key: str = "") -> dict:
    # COMPENSABLE, not REVERSIBLE: the ticket keeps its history and its
    # notification already went to the project's watchers.
    if issue_key in WORLD.tickets:
        WORLD.tickets[issue_key]["status"] = "Closed as mistaken"
        WORLD.tickets.pop(issue_key)
    return {"issue_key": issue_key, "state": "closed"}


@compensator("demo.github.close_pull_request")
def close_pull_request(repo: str, number: int, idempotency_key: str = "") -> dict:
    WORLD.pulls.pop(f"{repo}#{number}", None)
    return {"repo": repo, "number": number, "state": "closed"}


# ===========================================================================
# The workflow
# ===========================================================================

CUSTOMER = "acme-corp"


async def onboard(saga, fail_at: int) -> None:
    """Provision a customer's search environment across five systems."""

    def maybe_fail(step: int) -> None:
        if step == fail_at:
            raise RuntimeError(
                f"step {step} failed: the agent picked an instance type that "
                f"does not exist in this region")

    # 1. Cloud & Infrastructure -- Terraform / AWS
    instance = await saga.execute(
        tool="aws.run_instances",
        semantics=C,                      # terminate is a new, billable event
        forward=lambda: _provision(),
        compensate=lambda r: Compensation(
            fn=terminate_instance, handler="demo.aws.terminate_instance",
            kwargs={"instance_id": r["instance_id"]},
            description=f"terminate {r['instance_id']}"),
        policy_args={"instance_type": "m6i.large", "region": "eu-west-1"},
    )
    maybe_fail(1)

    # 2. Data & AI Infrastructure -- Pinecone
    await saga.execute(
        tool="pinecone.create_namespace",
        semantics=C,
        forward=lambda: _create_namespace(),
        compensate=lambda r: Compensation(
            fn=delete_namespace, handler="demo.pinecone.delete_namespace",
            kwargs={"index": r["index"], "namespace": r["namespace"]},
            description=f"delete namespace {r['namespace']}"),
        policy_args={"index": "prod-search", "dimension": 1536},
    )
    maybe_fail(2)

    # 3. Enterprise SaaS -- Jira
    await saga.execute(
        tool="jira.create_issue",
        semantics=C,                      # watchers were already notified
        forward=lambda: _open_ticket(instance["instance_id"]),
        compensate=lambda r: Compensation(
            fn=close_issue, handler="demo.jira.close_issue",
            kwargs={"issue_key": r["issue_key"]},
            description=f"close {r['issue_key']}"),
        policy_args={"project": "ONB", "issue_type": "Task"},
    )
    maybe_fail(3)

    # 4. Developer Workflow -- GitHub
    await saga.execute(
        tool="github.create_pull_request",
        semantics=C,
        forward=lambda: _open_pr(),
        compensate=lambda r: Compensation(
            fn=close_pull_request, handler="demo.github.close_pull_request",
            kwargs={"repo": r["repo"], "number": r["number"]},
            description=f"close {r['repo']}#{r['number']}"),
        policy_args={"repo": "acme/infra", "base": "main"},
    )
    maybe_fail(4)

    # 5. Messaging -- an SMS that cannot be unsent.
    #
    # The default gate REQUIRES approval for IRREVERSIBLE steps and no approval
    # provider is configured, so this is refused *before* it runs. That refusal
    # is the product: cleaning up after a delivered message is not possible, so
    # the engine declines to start it.
    await saga.execute(
        tool="twilio.send_sms",
        semantics=I,
        forward=lambda: _send_sms(),
        policy_args={"to": "+15550100", "template": "onboarding_complete"},
    )


def _provision() -> dict:
    iid = f"i-{len(WORLD.instances) + 1:08x}"
    WORLD.instances[iid] = {"type": "m6i.large", "state": "running"}
    return {"instance_id": iid}


def _create_namespace() -> dict:
    key = "prod-search/acme"
    WORLD.namespaces[key] = {"vectors": 0, "dimension": 1536}
    return {"index": "prod-search", "namespace": "acme"}


def _open_ticket(instance_id: str) -> dict:
    key = f"ONB-{len(WORLD.tickets) + 101}"
    WORLD.tickets[key] = {"summary": f"Onboard {CUSTOMER} ({instance_id})",
                          "status": "Open"}
    return {"issue_key": key}


def _open_pr() -> dict:
    number = 4200 + len(WORLD.pulls)
    WORLD.pulls[f"acme/infra#{number}"] = {"title": f"Add {CUSTOMER} search env"}
    return {"repo": "acme/infra", "number": number}


def _send_sms() -> dict:
    WORLD.notified.append("+15550100")
    return {"sid": "SM-demo"}


# ===========================================================================

def _render(title: str) -> None:
    print(f"\n  {title}")
    for label, count, system in WORLD.summary():
        flag = " " if count == 0 else "!"
        print(f"    {flag} {label:<20} {count:>3}   ({system})")


async def main(fail_at: int) -> int:
    global WORLD
    WORLD = World()

    print()
    print("  agent-saga :: one agent, five systems, one transaction")
    print("  " + "-" * 66)
    print("  Onboarding a customer's search environment:")
    print("    aws.run_instances -> pinecone.create_namespace -> jira.create_issue")
    print("    -> github.create_pull_request -> twilio.send_sms")
    print(f"  Injected failure at step {fail_at}.")
    print("  " + "-" * 66)

    try:
        async with saga_scope(gate=PreFlightGate()) as saga:
            await onboard(saga, fail_at)
        print("\n  saga completed (no failure injected)")
    except SagaAborted as aborted:
        print(f"\n  ! {type(aborted.cause).__name__}: {aborted.cause}")
        print(f"\n  {'<<'} boundary intercepted -- compensating LIFO")
        for step in aborted.report.compensated:
            print(f"      OK  {step.tool:<32} {step.compensation.description}")
        for step in aborted.report.orphaned:
            print(f"      !!  {step.tool:<32} ORPHANED (nothing can undo it)")
        print(f"\n  {aborted.report.summary()}")

    _render("Resulting state across all five systems:")
    print()
    if WORLD.is_clean:
        print("  CLEAN -- every system is back where it started.")
        print("  Note the SMS was never sent: the gate refused an IRREVERSIBLE")
        print("  step up front rather than trying to clean up a delivered message.")
    else:
        print("  INCOMPLETE -- see the rollback report above.")
    print()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fail-at", type=int, default=4, choices=[1, 2, 3, 4, 5],
                    help="which step raises (default: 4)")
    raise SystemExit(asyncio.run(main(ap.parse_args().fail_at)))
