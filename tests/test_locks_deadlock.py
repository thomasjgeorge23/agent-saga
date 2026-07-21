import pytest
import asyncio
from agent_saga import saga_scope, SagaAborted, LockAcquisitionTimeoutError, get_semantic_locks


@pytest.mark.anyio
async def test_deadlock_resolution_timeout():
    locks = get_semantic_locks()
    
    # Reset lock state
    locks._owners.clear()
    
    a_acquired_1 = asyncio.Event()
    b_acquired_2 = asyncio.Event()
    
    a_aborted = asyncio.Event()
    b_completed = asyncio.Event()
    
    async def task_a():
        try:
            async with saga_scope() as ctx:
                await ctx.acquire_semantic_lock("resource_1")
                a_acquired_1.set()
                
                # Wait for Saga B to acquire resource_2
                await b_acquired_2.wait()
                
                # Request resource_2 with a short timeout to force deadlock break
                await ctx.acquire_semantic_lock("resource_2", timeout=0.1)
        except SagaAborted as exc:
            assert isinstance(exc.cause, LockAcquisitionTimeoutError)
            a_aborted.set()
            
    async def task_b():
        async with saga_scope() as ctx:
            await ctx.acquire_semantic_lock("resource_2")
            b_acquired_2.set()
            
            # Wait for Saga A to acquire resource_1
            await a_acquired_1.wait()
            
            # Try to acquire resource_1 with a longer timeout
            # Should succeed after Saga A aborts and releases resource_1
            await ctx.acquire_semantic_lock("resource_1", timeout=1.0)
            b_completed.set()
            
    # Run both concurrently
    await asyncio.gather(task_a(), task_b())
    
    assert a_aborted.is_set()
    assert b_completed.is_set()
    
    # Verify everything is cleanly released
    assert locks.owner("resource_1") is None
    assert locks.owner("resource_2") is None
