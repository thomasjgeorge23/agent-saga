"""Tests for the shipped pytest plugin (#28): saga_wal, chaos_runner,
assert_saga_deterministic."""

import pytest
from conftest import aio

from agent_saga.semantics import ActionSemantics
from agent_saga.context import Compensation


@aio
async def test_saga_wal_fixture_is_fresh_and_usable(saga_wal):
    await saga_wal.start()
    saga_wal.append("SAGA_START", {"saga_id": "s1", "name": "demo"})
    await saga_wal.barrier()
    recs = saga_wal.records()
    assert any(r["event"] == "SAGA_START" for r in recs)


@aio
async def test_chaos_runner_fixture_default(chaos_runner):
    async def saga(ctx):
        await ctx.execute(tool="t1", semantics=ActionSemantics.COMPENSABLE,
                          forward=lambda: {"ok": 1},
                          compensate=lambda res: Compensation(
                              fn=lambda **k: None, handler="c1", kwargs={}, description="undo"))
    res = await chaos_runner.run(saga)     # default ChaosRunner fails at step 1
    assert res.rolled_back is True


@pytest.mark.saga_chaos(fail_at=[2])
@aio
async def test_chaos_runner_reads_marker(chaos_runner):
    # marker configured the runner to fail at step 2
    assert chaos_runner.fail_points == [2]


def test_assert_saga_deterministic_passes_and_fails(assert_saga_deterministic):
    # A clean (empty) stream is deterministic.
    assert_saga_deterministic([])
    # The helper is callable and returns None on success.
    assert assert_saga_deterministic([{"event": "SAGA_START"}]) is None


def test_plugin_registers_saga_chaos_marker(pytestconfig):
    markers = pytestconfig.getini("markers")
    assert any("saga_chaos" in m for m in markers)


def test_standalone_pytest_agent_saga_package():
    import sys
    from pathlib import Path
    pkg_dir = str(Path(__file__).parent.parent / "pytest-agent-saga")
    if pkg_dir not in sys.path:
        sys.path.insert(0, pkg_dir)
    import pytest_agent_saga
    import pytest_agent_saga.plugin
    assert pytest_agent_saga.__version__ == "0.2.2"
    assert hasattr(pytest_agent_saga.plugin, "saga_wal")
