"""Bounded model checking of the rollback state machine, as a test.

The criticism this answers is fair: "tests check the cases somebody thought of."
So this checks every failure shape up to N steps -- which step's forward call
raises, which subset of the inverses then refuse, and whether a committed step
has no inverse at all -- against the real engine rather than a model of it.

The bound is the honest part. It says nothing about six steps, concurrency, or
partitions; `test_chaos.py`, the `crash_worker` subprocesses and
`test_mesh_fuzz.py` cover those.
"""

import logging

import pytest
from conftest import aio

from agent_saga.verification import INVARIANTS, verify_rollback_invariants


@pytest.fixture(autouse=True)
def _quiet():
    """The engine narrates every rollback at ERROR; 49 deliberate failures make
    that unreadable."""
    logger = logging.getLogger("agent_saga")
    previous = logger.level
    logger.setLevel(logging.CRITICAL)
    yield
    logger.setLevel(previous)


@aio
async def test_every_failure_shape_up_to_four_steps_holds_the_invariants():
    report = await verify_rollback_invariants(max_steps=4)

    assert report.interleavings == 49
    assert report.verified, report.format_text()
    assert len(report.invariants) == len(INVARIANTS)


@aio
async def test_the_bound_extends_to_six_steps():
    """321 interleavings, still exhaustive, still under a second -- so there is
    no reason for the default suite to check a smaller space than it can."""
    report = await verify_rollback_invariants(max_steps=6)
    assert report.interleavings == 321
    assert report.verified, report.format_text()


@aio
async def test_it_also_holds_without_halt_on_compensation_failure():
    """The other configuration: keep unwinding past a failed inverse instead of
    stranding what is left."""
    report = await verify_rollback_invariants(
        max_steps=3, halt_on_compensation_failure=False)
    assert report.verified, report.format_text()


@aio
async def test_the_report_states_its_bound_rather_than_claiming_proof():
    """'Verified' with no bound attached is the kind of claim this package
    exists to avoid."""
    report = await verify_rollback_invariants(max_steps=2)
    text = " ".join(report.format_text().split())

    assert "bounded: N <= 2" in text
    assert "not a proof for unbounded N" in text
    assert "not a claim about concurrency" in text


@aio
async def test_a_verification_over_zero_cases_is_not_a_pass():
    """Vacuous success, refused as everywhere else in this package."""
    report = await verify_rollback_invariants(max_steps=1)
    assert report.interleavings > 0
    assert report.verified

    from agent_saga.verification import VerificationReport
    empty = VerificationReport(max_steps=4, interleavings=0, violations=(),
                              invariants=INVARIANTS)
    assert not empty.verified


@aio
async def test_the_report_is_machine_readable():
    data = (await verify_rollback_invariants(max_steps=2)).describe()
    assert data["max_steps"] == 2
    assert data["verified"] is True
    assert data["violations"] == []
    assert len(data["invariants"]) == len(INVARIANTS)


def test_a_nonsense_bound_is_refused():
    import asyncio
    with pytest.raises(ValueError, match="max_steps must be >= 1"):
        asyncio.run(verify_rollback_invariants(max_steps=0))
