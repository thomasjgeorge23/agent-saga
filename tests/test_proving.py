"""prove_rollback: break the workflow at every step and check the world.

The claim under test is narrow and strong: for a workflow whose compensations
are correct, every failure point unwinds to the starting world -- and for one
whose compensations are subtly wrong, the prover says exactly which step and
what it left behind.

The most valuable case is the last one: a compensation that runs, returns
successfully, and undoes nothing. The engine reports `clean` because its
inverse did not raise. Only comparing the actual world catches it.
"""

import pytest
from conftest import aio

from agent_saga import ActionSemantics, Compensation
from agent_saga.proving import ProbeMode, prove_rollback


class World:
    def __init__(self):
        self.charges = []
        self.servers = []

    def snapshot(self):
        return {"charges": sorted(self.charges), "servers": sorted(self.servers)}

    def reset(self):
        self.charges.clear()
        self.servers.clear()


def make_scenario(world: World, *, broken_step: int = 0, silent: bool = False):
    """A three-step workflow. `broken_step` makes that step's compensation a
    no-op; `silent` makes it succeed while doing nothing, which is the case
    that fools the engine."""

    def charge(amount):
        cid = f"ch_{len(world.charges) + 1}"
        world.charges.append(cid)
        return {"id": cid}

    def launch(size):
        sid = f"i-{len(world.servers) + 1}"
        world.servers.append(sid)
        return {"id": sid}

    def configure(target):
        return {"configured": target}

    def refund(charge_id):
        if broken_step == 1:
            if silent:
                return {"refunded": charge_id}      # says yes, does nothing
            raise RuntimeError("refund endpoint down")
        world.charges = [c for c in world.charges if c != charge_id]

    def terminate(server_id):
        if broken_step == 2:
            if silent:
                return {"terminated": server_id}
            raise RuntimeError("terminate failed")
        world.servers = [s for s in world.servers if s != server_id]

    async def scenario(ctx):
        c = await ctx.execute(
            tool="stripe.charge", semantics=ActionSemantics.COMPENSABLE,
            forward=charge, forward_kwargs={"amount": 100},
            compensate=lambda r: Compensation(
                fn=refund, kwargs={"charge_id": r["id"]}, description="refund"))
        s = await ctx.execute(
            tool="aws.launch", semantics=ActionSemantics.COMPENSABLE,
            forward=launch, forward_kwargs={"size": "m6i"},
            compensate=lambda r: Compensation(
                fn=terminate, kwargs={"server_id": r["id"]}, description="terminate"))
        await ctx.execute(
            tool="provision.configure", semantics=ActionSemantics.COMPENSABLE,
            forward=configure, forward_kwargs={"target": s["id"]},
            compensate=lambda r: Compensation(
                fn=lambda: None, description="nothing durable to undo"))
        return c, s

    return scenario


# -- a correct workflow proves clean at every failure point ------------------------

@aio
async def test_a_correct_workflow_is_proven_at_every_step():
    world = World()
    proof = await prove_rollback(make_scenario(world),
                                 snapshot=world.snapshot, reset=world.reset)

    assert proof.steps == 3
    assert len(proof.probes) == 6                 # 3 steps x before/after
    assert proof.proven, proof.format_text()
    assert not proof.failures and not proof.lies
    assert all(p.verdict == "CLEAN" for p in proof.probes)


@aio
async def test_both_failure_shapes_are_probed():
    world = World()
    proof = await prove_rollback(make_scenario(world),
                                 snapshot=world.snapshot, reset=world.reset)
    modes = {(p.index, p.mode) for p in proof.probes}
    assert (1, ProbeMode.BEFORE) in modes
    assert (1, ProbeMode.AFTER) in modes
    assert {p.tool for p in proof.probes} == {
        "stripe.charge", "aws.launch", "provision.configure"}


@aio
async def test_probing_only_one_mode_is_supported():
    world = World()
    proof = await prove_rollback(make_scenario(world), snapshot=world.snapshot,
                                 reset=world.reset, modes=(ProbeMode.AFTER,))
    assert len(proof.probes) == 3
    assert all(p.mode == ProbeMode.AFTER for p in proof.probes)


# -- a broken compensation is located precisely -------------------------------------

@aio
async def test_a_raising_compensation_is_reported_dirty_with_its_residue():
    world = World()
    proof = await prove_rollback(make_scenario(world, broken_step=1),
                                 snapshot=world.snapshot, reset=world.reset)

    assert not proof.proven
    dirty = proof.failures
    assert dirty, proof.format_text()
    # the charge could not be refunded, so it is left behind
    assert any("charges" in (p.residue or "") for p in dirty)
    # the engine also noticed -- it raised, so this is DIRTY, not a lie
    assert all(p.verdict == "DIRTY" for p in dirty)


@aio
async def test_a_silent_no_op_compensation_is_caught_as_a_lie():
    """The case nothing else catches. The inverse returns successfully and
    undoes nothing, so RollbackReport says clean while the world still holds
    the charge."""
    world = World()
    proof = await prove_rollback(make_scenario(world, broken_step=1, silent=True),
                                 snapshot=world.snapshot, reset=world.reset)

    assert not proof.proven
    assert proof.lies, proof.format_text()
    lie = proof.lies[0]
    assert lie.engine_said_clean is True          # the engine was satisfied
    assert lie.world_restored is False            # the world was not
    assert "ch_1" in (lie.residue or "")
    assert "LIES" in proof.format_text()


@aio
async def test_the_failing_step_is_named():
    world = World()
    proof = await prove_rollback(make_scenario(world, broken_step=2, silent=True),
                                 snapshot=world.snapshot, reset=world.reset)
    # step 2's inverse is broken, so any probe that got past step 2 is dirty
    assert all(p.index >= 2 for p in proof.failures)
    assert any("servers" in (p.residue or "") for p in proof.failures)


# -- reporting ------------------------------------------------------------------------

@aio
async def test_the_report_is_readable_and_machine_readable():
    world = World()
    proof = await prove_rollback(make_scenario(world),
                                 snapshot=world.snapshot, reset=world.reset)

    text = proof.format_text()
    assert "rollback proof: 6 probe(s) across 3 step(s)" in text
    assert "proven: True" in text

    data = proof.describe()
    assert data["proven"] is True
    assert data["probes_run"] == 6
    assert data["failures"] == 0 and data["lies"] == 0
    assert len(data["probes"]) == 6


@aio
async def test_a_workflow_with_no_steps_proves_nothing_and_says_so():
    """An empty proof must never read as a pass -- `proven` is False and the
    report says why, rather than reporting vacuous success."""
    async def nothing(ctx):
        return None

    proof = await prove_rollback(nothing, snapshot=lambda: {}, reset=lambda: None)
    assert proof.steps == 0
    assert proof.probes == ()
    assert proof.proven is False
    assert "nothing was proven" in proof.format_text()


@aio
async def test_max_steps_bounds_a_long_workflow():
    world = World()
    proof = await prove_rollback(make_scenario(world), snapshot=world.snapshot,
                                 reset=world.reset, max_steps=2,
                                 modes=(ProbeMode.BEFORE,))
    assert proof.steps == 2
    assert len(proof.probes) == 2
