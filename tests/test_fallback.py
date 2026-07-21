import pytest
import asyncio
from agent_saga import ActionSemantics, Compensation, SagaContext, saga
from agent_saga.wal import FileWAL
from agent_saga.retry import RetryPolicy

C = ActionSemantics.COMPENSABLE


@pytest.mark.anyio
async def test_fallback_action_executes_on_failure_and_swallows_exception():
    # Setup mock tracking
    fallback_called = False
    step_2_called = False
    compensation_run = False

    def fallback():
        nonlocal fallback_called
        fallback_called = True
        return "default_mock_state"

    def compensate_step_2(res):
        def undo():
            nonlocal compensation_run
            compensation_run = True
        return Compensation(fn=undo)

    @saga.step(semantics=C, fallback_action=fallback)
    async def step_1():
        raise ConnectionError("primary step failed")

    @saga.step(semantics=C, compensate=compensate_step_2)
    async def step_2():
        nonlocal step_2_called
        step_2_called = True
        return "step_2_success"

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        wal = FileWAL(Path(tmpdir) / "wal.jsonl")
        await wal.start()
        
        try:
            @saga(wal=wal)
            async def run_saga():
                r1 = await step_1()
                r2 = await step_2()
                return r1, r2

            # Run the saga
            res1, res2 = await run_saga()
            
            # Verify outcomes
            assert res1 == "default_mock_state"
            assert res2 == "step_2_success"
            assert fallback_called is True
            assert step_2_called is True
            assert compensation_run is False  # No rollback triggered!
            
            # Verify WAL records contain COMPLETED_VIA_FALLBACK
            records = await wal.read_all()
            events = [r["event"] for r in records]
            assert "COMPLETED_VIA_FALLBACK" in events
            assert "SAGA_ABORTED" not in events
            assert "ROLLBACK_START" not in events
        finally:
            await wal.close()


@pytest.mark.anyio
async def test_fallback_action_async_executes_successfully():
    async def async_fallback():
        await asyncio.sleep(0.01)
        return "async_default_mock_state"

    @saga.step(semantics=C, fallback_action=async_fallback)
    async def failing_step():
        raise ValueError("failed")

    wal = FileWAL()
    await wal.start()
    try:
        @saga(wal=wal)
        async def run_saga():
            return await failing_step()

        res = await run_saga()
        assert res == "async_default_mock_state"
    finally:
        await wal.close()


@pytest.mark.anyio
async def test_failed_fallback_triggers_rollback():
    from agent_saga import SagaAborted

    fallback_called = False
    step_1_compensated = False

    def bad_fallback():
        nonlocal fallback_called
        fallback_called = True
        raise RuntimeError("fallback also failed")

    def compensate_step_1(res):
        def undo():
            nonlocal step_1_compensated
            step_1_compensated = True
        return Compensation(fn=undo)

    @saga.step(semantics=C, compensate=compensate_step_1)
    async def step_1():
        return "success"

    @saga.step(semantics=C, fallback_action=bad_fallback)
    async def step_2_failing():
        raise ValueError("failing")

    wal = FileWAL()
    await wal.start()
    try:
        @saga(wal=wal)
        async def run_saga():
            await step_1()
            await step_2_failing()

        with pytest.raises(SagaAborted) as exc_info:
            await run_saga()
        
        report = exc_info.value.report
        assert report.clean is False
        assert fallback_called is True
        assert step_1_compensated is True
    finally:
        await wal.close()


@pytest.mark.anyio
async def test_fallback_action_outside_saga_boundary():
    fallback_called = False

    def fallback():
        nonlocal fallback_called
        fallback_called = True
        return "outside_fallback_state"

    @saga.step(semantics=C, fallback_action=fallback)
    async def step_failing():
        raise ConnectionError("failed")

    # Call directly without active saga context
    res = await step_failing()
    assert res == "outside_fallback_state"
    assert fallback_called is True
