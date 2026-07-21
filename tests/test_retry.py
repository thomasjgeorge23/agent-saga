import pytest
import asyncio
from agent_saga import saga, SagaAborted, RetryPolicy, Compensation
from agent_saga.semantics import ActionSemantics

C = ActionSemantics.COMPENSABLE


@pytest.mark.anyio
async def test_flaky_api_retry_success():
    calls = 0
    compensated = []

    # Retry policy with 3 max retries (total 4 attempts allowed), linear delay (0.01s base)
    policy = RetryPolicy(max_retries=3, backoff_type="linear", base_delay=0.01)

    @saga.step(semantics=C, retry_policy=policy, compensate=lambda r: Compensation(fn=lambda: compensated.append(r)))
    async def flaky_step(val: str):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ValueError("flaky api error")
        return f"success-{val}"

    # Run the step in a saga
    @saga
    async def run_flow():
        return await flaky_step(val="test")

    res = await run_flow()
    assert res == "success-test"
    assert calls == 3  # attempt 1 failed, attempt 2 failed, attempt 3 succeeded
    assert len(compensated) == 0  # No rollback/compensation triggered because it succeeded


@pytest.mark.anyio
async def test_hard_failure_exceeds_retries_triggers_rollback():
    calls = 0
    compensated = []

    # Retry policy allowing 2 retries (total 3 attempts)
    policy = RetryPolicy(max_retries=2, backoff_type="exponential", base_delay=0.01)

    @saga.step(semantics=C, retry_policy=policy, compensate=lambda r: Compensation(fn=lambda: compensated.append("undone")))
    async def failing_step():
        nonlocal calls
        calls += 1
        raise RuntimeError("persistent api error")

    @saga
    async def run_flow():
        await failing_step()

    # Should raise SagaAborted wrapping the RuntimeError
    with pytest.raises(SagaAborted) as exc_info:
        await run_flow()

    assert isinstance(exc_info.value.cause, RuntimeError)
    assert calls == 3  # initial attempt + 2 retries = 3 total attempts
    assert compensated == ["undone"]  # rollback triggers because all retries failed


@pytest.mark.anyio
async def test_retry_on_filtered_exception():
    calls = 0
    compensated = []

    # Retry only on TimeoutError, exclude ValueError
    policy = RetryPolicy(
        max_retries=2,
        base_delay=0.01,
        retry_on=[TimeoutError],
        exclude_exceptions=[ValueError]
    )

    @saga.step(semantics=C, retry_policy=policy, compensate=lambda r: Compensation(fn=lambda: compensated.append("undone")))
    async def flaky_timeout_step():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise TimeoutError("timeout occurred")
        return "ok"

    @saga
    async def run_flow():
        return await flaky_timeout_step()

    res = await run_flow()
    assert res == "ok"
    assert calls == 3
    assert len(compensated) == 0


@pytest.mark.anyio
async def test_exclude_exception_bypasses_retry_immediately():
    calls = 0
    compensated = []

    # Retry only on TimeoutError, exclude ValueError
    policy = RetryPolicy(
        max_retries=2,
        base_delay=0.01,
        retry_on=[TimeoutError],
        exclude_exceptions=[ValueError]
    )

    @saga.step(semantics=C, retry_policy=policy, compensate=lambda r: Compensation(fn=lambda: compensated.append("undone")))
    async def immediate_fail_step():
        nonlocal calls
        calls += 1
        raise ValueError("unrecoverable error")

    @saga
    async def run_flow():
        await immediate_fail_step()

    with pytest.raises(SagaAborted) as exc_info:
        await run_flow()

    assert isinstance(exc_info.value.cause, ValueError)
    assert calls == 1  # Should fail on first attempt without retrying
    assert compensated == ["undone"]  # Should trigger compensation immediately
