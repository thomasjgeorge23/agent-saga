"""Multi-domain sagas: the engine is not a payments library.

One agent run crossing infrastructure, data, SaaS, developer workflow and
messaging. These tests exist to pin that the semantics, the gate, LIFO ordering
and crash recovery behave identically whatever the side effect happens to be.
"""

import tempfile
import time
from pathlib import Path

import pytest

from agent_saga import (
    ActionSemantics,
    AsyncWAL,
    Compensation,
    PreFlightGate,
    PreFlightViolation,
    Rule,
    SagaContext,
    Verdict,
    arg_exceeds,
)
from agent_saga.recovery import RecoveryDaemon, Resolution
from agent_saga.registry import compensator
from conftest import aio

R = ActionSemantics.REVERSIBLE
C = ActionSemantics.COMPENSABLE
I = ActionSemantics.IRREVERSIBLE

WORLD: dict = {}
UNDONE: list = []


@compensator("md.terminate_instance")
def terminate_instance(instance_id, idempotency_key=None):
    WORLD.pop(f"ec2:{instance_id}", None)
    UNDONE.append(f"ec2:{instance_id}")


@compensator("md.delete_namespace")
def delete_namespace(namespace, idempotency_key=None):
    WORLD.pop(f"pinecone:{namespace}", None)
    UNDONE.append(f"pinecone:{namespace}")


@compensator("md.close_issue")
def close_issue(issue_key, idempotency_key=None):
    WORLD.pop(f"jira:{issue_key}", None)
    UNDONE.append(f"jira:{issue_key}")


@compensator("md.close_pr")
def close_pr(number, idempotency_key=None):
    WORLD.pop(f"gh:{number}", None)
    UNDONE.append(f"gh:{number}")


@compensator("md.delete_message")
def delete_message(ts, idempotency_key=None):
    WORLD.pop(f"slack:{ts}", None)
    UNDONE.append(f"slack:{ts}")


@pytest.fixture(autouse=True)
def _fresh():
    WORLD.clear()
    UNDONE.clear()
    yield


async def _ctx(tmp: Path, gate=None):
    wal = AsyncWAL(tmp / "wal.jsonl")
    await wal.start()
    return SagaContext(gate=gate, wal=wal), wal


# Each domain, with the semantics it actually deserves.
DOMAIN_STEPS = [
    # (tool, semantics, key, handler, kwargs)
    ("aws.run_instances", C, "ec2:i-abc", "md.terminate_instance", {"instance_id": "i-abc"}),
    ("pinecone.create_namespace", C, "pinecone:acme", "md.delete_namespace", {"namespace": "acme"}),
    ("jira.create_issue", C, "jira:ONB-1", "md.close_issue", {"issue_key": "ONB-1"}),
    ("github.create_pull_request", C, "gh:4200", "md.close_pr", {"number": 4200}),
    ("slack.post_message", C, "slack:171.5", "md.delete_message", {"ts": "171.5"}),
]


async def _run_all_domains(ctx):
    for tool, semantics, key, handler, kwargs in DOMAIN_STEPS:
        await ctx.execute(
            tool=tool, semantics=semantics,
            forward=lambda k=key: WORLD.setdefault(k, {"created": True}) and k or k,
            compensate=lambda r, h=handler, kw=kwargs: Compensation(
                fn=lambda **_: None, handler=h, kwargs=kw,
                description=f"undo {h}"),
            policy_args={"tool": tool},
        )


# ==========================================================================
# One transaction across five domains
# ==========================================================================

@aio
async def test_a_five_domain_saga_unwinds_every_system_in_reverse():
    with tempfile.TemporaryDirectory() as d:
        ctx, wal = await _ctx(Path(d))
        await ctx.begin()
        await _run_all_domains(ctx)
        assert len(WORLD) == 5, "all five systems were mutated"

        # Rewire the compensations to the real registry handlers so the undo
        # actually mutates WORLD (mirrors how a connector is written).
        for step, (_t, _s, _k, handler, kwargs) in zip(ctx.stack, DOMAIN_STEPS):
            from agent_saga.registry import resolve
            step.compensation = Compensation(
                fn=resolve(handler), handler=handler, kwargs=kwargs)

        report = await ctx.rollback()
        await ctx.finish(aborted=True, clean=report.clean)
        await wal.close()

    assert report.clean
    assert WORLD == {}, "every system restored"
    # LIFO: messaging undone first, infrastructure last.
    assert UNDONE == ["slack:171.5", "gh:4200", "jira:ONB-1",
                      "pinecone:acme", "ec2:i-abc"]


@aio
async def test_the_gate_blocks_an_irreversible_notification_before_it_sends():
    """A delivered SMS cannot be recalled, so it is refused up front -- the same
    stance the engine takes on a wire transfer."""
    sent = []
    with tempfile.TemporaryDirectory() as d:
        ctx, wal = await _ctx(Path(d), gate=PreFlightGate())
        with pytest.raises(PreFlightViolation):
            await ctx.execute(tool="twilio.send_sms", semantics=I,
                              forward=lambda: sent.append("sms"),
                              policy_args={"to": "+15550100"})
        await wal.close()
    assert sent == [], "nothing was delivered"


@aio
async def test_a_threshold_rule_guards_infrastructure_not_just_money():
    """arg_exceeds is not a payments feature: it caps a replica count or a
    node pool exactly as it caps an amount."""
    gate = PreFlightGate(rules=[
        Rule("replica-cap", arg_exceeds("replicas", 50), Verdict.BLOCK,
             "Scaling beyond 50 replicas needs a human.")])
    scaled = []
    with tempfile.TemporaryDirectory() as d:
        ctx, wal = await _ctx(Path(d), gate=gate)
        await ctx.execute(tool="k8s.scale_deployment", semantics=C,
                          forward=lambda: scaled.append(10),
                          compensate=lambda r: Compensation(fn=lambda: None, handler="h"),
                          policy_args={"replicas": 10})
        with pytest.raises(PreFlightViolation, match="50 replicas"):
            await ctx.execute(tool="k8s.scale_deployment", semantics=C,
                              forward=lambda: scaled.append(500),
                              compensate=lambda r: Compensation(fn=lambda: None, handler="h"),
                              policy_args={"replicas": 500})
        await wal.close()
    assert scaled == [10], "the oversized scale never ran"


@aio
async def test_a_reversible_scratch_file_rides_the_fast_path():
    """Not every side effect is a network call. A file the saga created is
    genuinely REVERSIBLE and must not pay for a durability barrier."""
    from agent_saga import reversible

    with tempfile.TemporaryDirectory() as d:
        ctx, wal = await _ctx(Path(d))
        plan = {"resources": []}
        await reversible(ctx, target=plan,
                         mutate=lambda p: p["resources"].append("aws_instance.web"),
                         tool="terraform.plan")
        assert plan["resources"] == ["aws_instance.web"]
        assert wal.barriers == 0, "REVERSIBLE steps skip the fence"
        await ctx.rollback()
        await wal.close()
    assert plan["resources"] == []


# ==========================================================================
# Crash recovery is domain-agnostic
# ==========================================================================

@aio
async def test_a_crashed_infrastructure_saga_is_recovered_by_the_daemon():
    """The daemon does not know or care that the orphan is an EC2 instance
    rather than a charge."""
    import json

    old = time.time() - 3600
    with tempfile.TemporaryDirectory() as d:
        wal_path = Path(d) / "wal.jsonl"
        records = [
            {"seq": 1, "event": "SAGA_START", "saga_id": "s1", "ts": old,
             "pid": 1, "lease_ttl": 5.0},
            {"seq": 2, "event": "STEP_COMMITTED", "saga_id": "s1", "ts": old,
             "step_id": "st1", "tool": "aws.run_instances",
             "semantics": "COMPENSABLE",
             "compensation": {"handler": "md.terminate_instance",
                              "recoverable": True,
                              "kwargs": {"instance_id": "i-orphan"},
                              "idempotency_key": None, "fn": "t",
                              "description": ""}},
        ]
        with open(wal_path, "w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")

        WORLD["ec2:i-orphan"] = {"state": "running"}
        outcome = (await RecoveryDaemon(wal_path).recover_all())[0]

    assert outcome.resolution is Resolution.RECOVERED
    assert "ec2:i-orphan" in UNDONE
    assert WORLD == {}
