import asyncio
import os
import pytest
from unittest.mock import MagicMock, patch

from agent_saga.context import SagaContext
from agent_saga.semantics import ActionSemantics, Compensation
from agent_saga.wal import AsyncWAL, FileWAL, PostgresWAL
from agent_saga.serialization import saga_object_hook
from agent_saga.locks import FileLock
from conftest import aio


@aio
async def test_cancelled_error_does_not_trigger_fallback_or_breaker(tmp_path):
    wal = AsyncWAL(tmp_path / "wal.jsonl")
    await wal.start()

    ctx = SagaContext(wal=wal)
    fallback_called = False

    async def _forward():
        raise asyncio.CancelledError()

    async def _fallback():
        nonlocal fallback_called
        fallback_called = True
        return "fallback"

    with pytest.raises(asyncio.CancelledError):
        await ctx.execute(
            tool="test_tool",
            semantics=ActionSemantics.REVERSIBLE,
            forward=_forward,
            fallback_action=_fallback,
        )

    assert not fallback_called
    await wal.close()


@aio
async def test_file_wal_compaction_error_restores_handle(tmp_path):
    wal_path = tmp_path / "wal.jsonl"
    wal = FileWAL(wal_path)
    await wal.start()
    wal.append("STEP_INTENT", {"saga_id": "s1", "step_id": "st1"})
    await wal.barrier()

    with patch("os.replace", side_effect=PermissionError("Permission denied")):
        with pytest.raises(PermissionError):
            await wal.compact(keep_saga_ids=set())

    # File handle must be restored and usable
    assert wal._fh is not None
    wal.append("STEP_INTENT", {"saga_id": "s2", "step_id": "st2"})
    await wal.barrier()
    await wal.close()


def test_postgres_wal_invalid_table_identifier():
    with pytest.raises(ValueError, match="Invalid SQL table_name identifier"):
        PostgresWAL(table_name="invalid table; DROP TABLE users;--")


def test_serialization_disallowed_module_safelist():
    dangerous_payload = {
        "__type__": "pydantic",
        "__class__": "os.system",
        "value": {"command": "echo hacked"}
    }
    result = saga_object_hook(dangerous_payload)
    # Must return raw dict without attempting os.system import
    assert result == dangerous_payload["value"]


def test_file_lock_stale_cleanup(tmp_path):
    lock_dir = tmp_path / "claims"
    lock1 = FileLock(claims_dir=lock_dir, owner_id="owner1", ttl_seconds=0.1)
    assert lock1.acquire("resource_1")
    assert not lock1.acquire("resource_1")

    import time
    time.sleep(0.15)

    lock2 = FileLock(claims_dir=lock_dir, owner_id="owner2", ttl_seconds=0.1)
    assert lock2.acquire("resource_1")


def test_fernet_multi_key_rotation():
    from agent_saga.encryption import FernetEncryptor, generate_key

    old_key = generate_key()
    new_key = generate_key()

    # Encrypt payload with old key
    old_enc = FernetEncryptor(old_key)
    ciphertext = old_enc.encrypt(b"secret payload")

    # Instantiate rotated encryptor with new_key as primary, old_key as fallback
    rotated_enc = FernetEncryptor([new_key, old_key])

    # Should successfully decrypt historical ciphertext encrypted with old key
    assert rotated_enc.decrypt(ciphertext) == b"secret payload"


@aio
async def test_massive_multi_tenant_concurrency(tmp_path):
    from agent_saga.observability import current_correlation

    wal = AsyncWAL(tmp_path / "concurrent_wal.jsonl")
    await wal.start()

    async def _user_saga(user_idx: int):
        ctx = SagaContext(wal=wal, saga_id=f"user-{user_idx}")
        # Execute 5 steps per user concurrently
        for step_idx in range(5):
            def _forward(u=user_idx, s=step_idx):
                # Verify correlation context stays strictly isolated to this user/saga during execution
                saga_id, _ = current_correlation()
                assert saga_id == f"user-{u}"
                return {"user": u, "step": s}

            await ctx.execute(
                tool="user.action",
                semantics=ActionSemantics.COMPENSABLE,
                forward=_forward,
                compensate=lambda res: Compensation(fn=lambda: None, handler="user.undo", kwargs={}),
            )

    # Run 100 concurrent user sagas (500 step executions in parallel)
    tasks = [_user_saga(i) for i in range(100)]
    await asyncio.gather(*tasks)

    await wal.barrier()
    records = await wal.read_all()
    # 100 users * 5 steps = 500 records
    assert len(records) >= 500
    await wal.close()
