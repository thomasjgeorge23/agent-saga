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


# -- AutoLockHeartbeat ergonomics (context manager + explicit start/stop) -----

from conftest import aio
from agent_saga.locks import AutoLockHeartbeat, SemanticLockManager


@aio
async def test_autolockheartbeat_context_manager_renews():
    mgr = SemanticLockManager()
    mgr.try_acquire("acct:42", "saga-1")
    renews = []
    real = mgr.renew
    mgr.renew = lambda rid, sid, ttl: (renews.append(rid), real(rid, sid, ttl))[1]
    async with AutoLockHeartbeat("acct:42", "saga-1", manager=mgr, interval=0.05):
        await asyncio.sleep(0.17)
    assert len(renews) >= 2  # renewed multiple times across the operation


@aio
async def test_autolockheartbeat_explicit_start_stop_idempotent():
    mgr = SemanticLockManager()
    mgr.try_acquire("acct:7", "saga-2")
    hb = AutoLockHeartbeat("acct:7", "saga-2", manager=mgr, interval=0.05)
    await hb.start()
    await hb.start()          # second start is a no-op
    await asyncio.sleep(0.08)
    await hb.stop()
    await hb.stop()           # second stop is safe
    assert hb._task is None


@aio
async def test_autolockheartbeat_stops_when_lock_lost():
    # Manager that never granted the lock -> renew() returns False -> loop stops.
    mgr = SemanticLockManager()
    hb = AutoLockHeartbeat("acct:99", "saga-x", manager=mgr, interval=0.05)
    await hb.start()
    await asyncio.sleep(0.12)
    assert hb._task.done()
    await hb.stop()


@aio
async def test_autolockheartbeat_no_renew_backend_is_safe():
    class NoRenew:
        distributed = False
    hb = AutoLockHeartbeat("r", "s", manager=NoRenew(), interval=0.05)
    await hb.start()          # warns, does not crash or spawn a doomed task
    assert hb._task is None
    await hb.stop()


def test_autolockheartbeat_rejects_bad_interval():
    with pytest.raises(ValueError):
        AutoLockHeartbeat("r", "s", manager=SemanticLockManager(), interval=0)
