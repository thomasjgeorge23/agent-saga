import pytest
import asyncio
from agent_saga import get_semantic_locks

@pytest.mark.anyio
async def test_semantic_lock_heartbeat_renewal_and_destruction():
    mgr = get_semantic_locks()
    mgr._owners.clear()
    mgr._expirations.clear()
    mgr._heartbeats.clear()

    resource_id = "test_heartbeat_resource"
    saga_id = "saga-heartbeat-test"

    # Acquire lock with a very short TTL (2 seconds)
    await mgr.acquire(resource_id, saga_id, ttl=2.0)

    # Prove that the heartbeat task is spawned
    assert resource_id in mgr._heartbeats
    task = mgr._heartbeats[resource_id]
    assert not task.done()

    # Sleep for 5 seconds. If there were no heartbeat, the 2-second TTL lock would have expired.
    await asyncio.sleep(5.0)

    # Prove that because of the heartbeat, the lock has NOT expired and the owner is still saga_id
    assert mgr.owner(resource_id) == saga_id
    # Heartbeat task should still be running and not done/cancelled
    assert resource_id in mgr._heartbeats
    assert not mgr._heartbeats[resource_id].done()

    # Explicitly release the lock
    released = mgr.release(resource_id, saga_id)
    assert released is True

    # Prove that upon release, the background task is fully destroyed
    assert resource_id not in mgr._heartbeats
    # Give the event loop a chance to run so the cancel task finishes
    await asyncio.sleep(0.1)
    assert task.done() or task.cancelled()
