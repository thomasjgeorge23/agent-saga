"""v0.5.0 shipped three layers that claimed protection they did not provide.

Same family as the `patch_all()` defect the universal engine was fixed for: a
safety feature reporting success it did not achieve. These tests pin the
fixes, and each one fails against the 0.5.0 implementation.

1. `framework_wrappers` logged "inside agent-saga transaction boundary" and
   called the original method unchanged -- no transaction, no rollback.
2. `zkp` shipped a hardcoded key (so committed payloads were recoverable by
   anyone with the package), stored the root in place of a Merkle path, and
   "verified" by checking the root against itself -- accepting forgeries.
3. `SagaMeshCoordinator.rollback_mesh_saga` reported "ROLLED BACK cleanly"
   even when compensations raised or were missing entirely.
"""

import asyncio
import os

import pytest
from conftest import aio

from agent_saga import ActionSemantics, AgentKit, AsyncWAL, Compensation, SagaAborted
from agent_saga.adapters.framework_wrappers import (
    is_saga_wrapped,
    wrap_crew,
    wrap_langgraph,
    wrap_method,
)
from agent_saga.multi_agent_mesh import SagaMeshCoordinator
from agent_saga.zkp import (
    AuditCommitmentError,
    BlindAuditCommitments,
    ZeroKnowledgeAuditProof,
    ZKProofCommitment,
)

KEY = b"a-test-key-of-sufficient-length!"


# -- 1. the wrappers open a real boundary -----------------------------------------

class FakeCrew:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def kickoff(self, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("crew step blew up")
        return "crew result"


class FakeGraph:
    async def ainvoke(self, payload):
        raise RuntimeError("graph blew up")


def test_wrapping_is_verifiable_not_just_logged(tmp_path):
    crew = FakeCrew()
    assert not is_saga_wrapped(crew, "kickoff")
    wrap_crew(crew)
    assert is_saga_wrapped(crew, "kickoff")


def test_the_wrapped_call_still_returns_its_result(tmp_path):
    crew = wrap_crew(FakeCrew())
    assert crew.kickoff() == "crew result"
    assert crew.kickoff.__agent_saga_original__ is not None


def test_a_failure_inside_the_boundary_compensates_registered_steps(tmp_path):
    """The whole point. Under 0.5.0 the refund never ran, because there was no
    transaction -- the wrapper only logged that there was one."""
    undone = []

    def charge(amount):
        return {"id": "ch_1", "amount": amount}

    kit = AgentKit(name="crew-test")
    safe_charge = kit.safe_tool(
        charge, semantics="COMPENSABLE",
        compensate=lambda r: Compensation(
            fn=lambda charge_id: undone.append(charge_id),
            kwargs={"charge_id": r["id"]}, description="refund"))

    class Crew:
        def kickoff(self):
            asyncio.get_event_loop()          # inside the saga's loop
            raise AssertionError("replaced below")

    crew = Crew()

    async def _kickoff():
        await safe_charge(amount=4200)
        raise RuntimeError("crew failed after charging")

    crew.kickoff = _kickoff                   # an async entry point
    wrap_method(crew, "kickoff", kit=kit)

    async def _run():
        with pytest.raises(SagaAborted):
            await crew.kickoff()

    asyncio.run(_run())
    assert undone == ["ch_1"], "the boundary did not roll the charge back"


@aio
async def test_an_async_method_is_wrapped_and_aborts_as_a_saga():
    graph = wrap_langgraph(FakeGraph())
    assert is_saga_wrapped(graph, "ainvoke")
    with pytest.raises(SagaAborted):
        await graph.ainvoke({"x": 1})


def test_wrapping_is_idempotent():
    crew = FakeCrew()
    wrap_crew(crew)
    first = crew.kickoff
    wrap_crew(crew)
    assert crew.kickoff is first          # not a second nested boundary


def test_an_object_with_no_wrappable_method_is_refused():
    """0.5.0 returned the object untouched, so a framework version bump gave
    you an unprotected object that looked protected."""
    class Nothing:
        pass

    with pytest.raises(AttributeError, match="nothing to wrap"):
        wrap_crew(Nothing())


@aio
async def test_a_sync_method_refuses_inside_a_running_loop():
    crew = wrap_crew(FakeCrew())
    with pytest.raises(RuntimeError, match="running event loop"):
        crew.kickoff()


# -- 2. the commitments are real ------------------------------------------------------

def test_a_key_is_required_and_must_be_strong():
    """The 0.5.0 default was compiled into the published package, so every
    commitment made with it was recoverable by anyone who pip-installed."""
    with pytest.raises(AuditCommitmentError, match="required"):
        ZeroKnowledgeAuditProof()
    with pytest.raises(AuditCommitmentError, match="at least"):
        BlindAuditCommitments(b"short")
    with pytest.raises(AuditCommitmentError, match="must be bytes"):
        BlindAuditCommitments("a string that is long enough to pass length")


def test_a_forged_commitment_no_longer_verifies():
    """The 0.5.0 check was `expected_root in commitment.merkle_path`, and
    merkle_path had been set to [root] -- so anything verified."""
    engine = BlindAuditCommitments(KEY)
    root, commitments = engine.build([
        {"saga_id": "tx1", "event": "PAY", "payload": {"amount": 500}},
        {"saga_id": "tx2", "event": "SHIP", "payload": {"x": 1}},
    ])

    forged = ZKProofCommitment(
        saga_id="tx_EVIL", event_type="NEVER_HAPPENED",
        commitment_hash="0" * 64, proof=commitments[0].proof)
    assert engine.verify(forged, root) is False


def test_a_real_commitment_does_not_verify_against_another_tree():
    engine = BlindAuditCommitments(KEY)
    root_a, commits_a = engine.build([{"saga_id": "a", "event": "E", "payload": {}},
                                      {"saga_id": "b", "event": "E", "payload": {}}])
    root_b, _ = engine.build([{"saga_id": "z", "event": "E", "payload": {}}])
    assert engine.verify(commits_a[0], root_a) is True
    assert engine.verify(commits_a[0], root_b) is False


@pytest.mark.parametrize("size", [1, 2, 3, 4, 5, 8, 9, 17])
def test_every_leaf_verifies_at_any_tree_size(size):
    """Odd levels duplicate the last node; the proof must reproduce that
    shape or leaves near the end fail."""
    engine = BlindAuditCommitments(KEY)
    records = [{"saga_id": f"tx{i}", "event": "E", "payload": {"i": i}}
               for i in range(size)]
    root, commitments = engine.build(records)
    assert len(commitments) == size
    for commitment in commitments:
        assert engine.verify(commitment, root) is True


def test_the_key_holder_can_bind_a_commitment_to_its_payload():
    engine = BlindAuditCommitments(KEY)
    records = [{"saga_id": "tx1", "event": "PAY", "payload": {"amount": 500}}]
    root, commitments = engine.build(records)

    assert engine.verify_record(records[0], commitments[0], root) is True
    tampered = {"saga_id": "tx1", "event": "PAY", "payload": {"amount": 5000}}
    assert engine.verify_record(tampered, commitments[0], root) is False


def test_a_different_key_produces_different_commitments():
    a = BlindAuditCommitments(KEY).commit("tx", "E", {"v": 1})
    b = BlindAuditCommitments(b"a-different-key-also-long-enough").commit("tx", "E", {"v": 1})
    assert a != b


def test_field_separators_cannot_be_confused():
    """Length-prefixed fields, so ('a:b','c') and ('a','b:c') cannot collide."""
    engine = BlindAuditCommitments(KEY)
    assert engine.commit("a:b", "c", {}) != engine.commit("a", "b:c", {})


def test_a_commitment_without_a_proof_raises_rather_than_returning_false():
    engine = BlindAuditCommitments(KEY)
    bare = ZKProofCommitment(saga_id="x", event_type="E", commitment_hash="0" * 64)
    with pytest.raises(AuditCommitmentError, match="no inclusion proof"):
        engine.verify(bare, "0" * 64)


def test_the_legacy_class_still_works_with_an_explicit_key():
    legacy = ZeroKnowledgeAuditProof(audit_salt=KEY)
    root, commitments = legacy.generate_proof_tree(
        [{"saga_id": "tx1", "event": "PAY", "payload": {"amount": 1}}])
    assert legacy.verify_commitment_against_root(commitments[0], root) is True


# -- 3. the mesh reports a dirty rollback as dirty -------------------------------------

@aio
async def test_a_failed_compensation_is_not_reported_as_clean():
    mesh = SagaMeshCoordinator(node_id="n1")
    await mesh.prepare_distributed_saga("m1", ["agent_a", "agent_b"])

    def boom(**kwargs):
        raise RuntimeError("peer unreachable")

    await mesh.register_participant_step(
        "m1", "agent_a", "reserve", lambda **kw: {"ok": True}, {"sku": "x"},
        compensate_fn=boom)

    report = await mesh.rollback_mesh_saga("m1")
    assert report["clean"] is False
    assert report["state"] == "ROLLED_BACK_PARTIAL"
    assert report["failed_steps"] and report["failed_steps"][0]["agent_id"] == "agent_a"


@aio
async def test_a_step_with_no_compensation_is_orphaned_not_counted_as_undone():
    mesh = SagaMeshCoordinator(node_id="n1")
    await mesh.prepare_distributed_saga("m2", ["agent_a"])
    await mesh.register_participant_step(
        "m2", "agent_a", "send_email", lambda **kw: {"sent": True}, {"to": "x"},
        compensate_fn=None)

    report = await mesh.rollback_mesh_saga("m2")
    assert report["clean"] is False
    assert report["compensated_steps"] == 0
    assert report["orphaned_steps"] == [{"agent_id": "agent_a", "step": "send_email"}]


@aio
async def test_a_genuinely_clean_rollback_still_reports_clean():
    undone = []
    mesh = SagaMeshCoordinator(node_id="n1")
    await mesh.prepare_distributed_saga("m3", ["agent_a"])
    await mesh.register_participant_step(
        "m3", "agent_a", "reserve", lambda **kw: {"ok": True}, {"sku": "x"},
        compensate_fn=lambda **kw: undone.append(kw))

    report = await mesh.rollback_mesh_saga("m3")
    assert report["clean"] is True
    assert report["state"] == "ROLLED_BACK"
    assert report["compensated_steps"] == 1
    assert undone == [{"sku": "x"}]
