"""Pluggable WAL backends: the BaseWAL contract, the barrier timeout, and the
Redis backend against a mocked client (no live Redis required in CI)."""

import asyncio
import json
import sys
import tempfile
import types
from pathlib import Path

import pytest

from agent_saga import ActionSemantics, Compensation, SagaContext
from agent_saga.wal import (
    AsyncWAL,
    BackpressurePolicy,
    BaseWAL,
    FileWAL,
    WALStalled,
)
from agent_saga.wal.redis_wal import RedisWAL
from conftest import aio

C = ActionSemantics.COMPENSABLE


# ==========================================================================
# A fake redis.asyncio client
# ==========================================================================

class FakeRedis:
    """The slice of redis.asyncio the backend actually uses."""

    def __init__(self, *, wait_acks=None, fail_on=None):
        self.lists: dict[str, list[str]] = {}
        self.calls: list[tuple] = []
        self.closed = False
        self._wait_acks = wait_acks
        self._fail_on = fail_on          # substring that makes rpush hang/raise

    async def rpush(self, key, *values):
        self.calls.append(("rpush", key, values))
        if self._fail_on and any(self._fail_on in v for v in values):
            raise ConnectionError("redis unreachable")
        self.lists.setdefault(key, []).extend(values)
        return len(self.lists[key])

    async def lrange(self, key, start, end):
        self.calls.append(("lrange", key, start, end))
        items = self.lists.get(key, [])
        return items if end == -1 else items[start:end + 1]

    async def delete(self, key):
        self.calls.append(("delete", key))
        self.lists.pop(key, None)
        return 1

    async def wait(self, replicas, timeout_ms):
        self.calls.append(("wait", replicas, timeout_ms))
        return replicas if self._wait_acks is None else self._wait_acks

    async def aclose(self):
        self.closed = True


# ==========================================================================
# The contract
# ==========================================================================

def test_file_and_redis_both_satisfy_the_base_contract():
    for cls in (FileWAL, RedisWAL):
        assert issubclass(cls, BaseWAL)
    # barrier() is part of the contract, not an optional extra: without a fence
    # a backend is fire-and-forget and a crash orphans real side effects.
    for name in ("append", "barrier", "read_all", "clear", "start", "close"):
        assert hasattr(BaseWAL, name), name


def test_asyncwal_remains_the_file_backend_for_backward_compatibility():
    assert AsyncWAL is FileWAL


def test_base_wal_cannot_be_instantiated():
    with pytest.raises(TypeError):
        BaseWAL()


# ==========================================================================
# Redis backend
# ==========================================================================

@aio
async def test_redis_wal_appends_and_reads_back_in_order():
    fake = FakeRedis()
    wal = RedisWAL(client=fake, key="test:wal")
    await wal.start()
    try:
        wal.append("SAGA_START", {"saga_id": "s1"})
        wal.append("STEP_COMMITTED", {"saga_id": "s1", "tool": "stripe.charge"})
        await wal.barrier()

        records = await wal.read_all()
        assert [r["event"] for r in records] == ["SAGA_START", "STEP_COMMITTED"]
        assert [r["seq"] for r in records] == [1, 2]
        assert records[1]["tool"] == "stripe.charge"
    finally:
        await wal.close()


@aio
async def test_redis_wal_group_commits_a_batch_into_one_rpush():
    fake = FakeRedis()
    wal = RedisWAL(client=fake, key="k")
    await wal.start()
    try:
        for i in range(5):
            wal.append("E", {"saga_id": "s", "i": i})
        await wal.barrier()
        rpushes = [c for c in fake.calls if c[0] == "rpush"]
        assert len(rpushes) == 1 and len(rpushes[0][2]) == 5
    finally:
        await wal.close()


@aio
async def test_redis_wal_clear_empties_the_key():
    fake = FakeRedis()
    wal = RedisWAL(client=fake, key="k")
    await wal.start()
    try:
        wal.append("E", {"saga_id": "s"})
        await wal.barrier()
        await wal.clear()
        assert await wal.read_all() == []
    finally:
        await wal.close()


@aio
async def test_redis_wal_waits_for_replicas_when_configured():
    """WAIT is the strongest fence Redis offers. It is not fsync, but it is the
    difference between 'the primary saw it' and 'a failover cannot lose it'."""
    fake = FakeRedis()
    wal = RedisWAL(client=fake, key="k", wait_replicas=2, wait_timeout_ms=500)
    await wal.start()
    try:
        wal.append("E", {"saga_id": "s"})
        await wal.barrier()
        assert ("wait", 2, 500) in fake.calls
    finally:
        await wal.close()


@aio
async def test_redis_wal_refuses_to_call_a_batch_durable_on_partial_replica_ack():
    fake = FakeRedis(wait_acks=0)          # no replica acknowledged
    wal = RedisWAL(client=fake, key="k", wait_replicas=2, barrier_timeout=1.0)
    await wal.start()
    try:
        wal.append("E", {"saga_id": "s"})
        with pytest.raises(WALStalled):
            await wal.barrier()
    finally:
        await wal.close()


@aio
async def test_injected_client_is_not_closed_by_the_wal():
    """An injected client belongs to the caller, who is probably sharing it."""
    fake = FakeRedis()
    wal = RedisWAL(client=fake, key="k")
    await wal.start()
    await wal.close()
    assert fake.closed is False


@aio
async def test_redis_wal_encrypts_records_when_a_key_is_configured():
    pytest.importorskip("cryptography")
    from agent_saga import FernetEncryptor, generate_key

    fake = FakeRedis()
    marker = "SENSITIVE_CUSTOMER_VALUE_42"
    wal = RedisWAL(client=fake, key="k", encryptor=FernetEncryptor(generate_key()))
    await wal.start()
    try:
        wal.append("STEP_INTENT", {"saga_id": "s", "note": marker})
        await wal.barrier()
        stored = fake.lists["k"]
        assert all(line.startswith("E1:") for line in stored)
        assert marker not in "".join(stored)       # ciphertext in Redis
        assert (await wal.read_all())[0]["note"] == marker   # readable with key
    finally:
        await wal.close()


# ==========================================================================
# The optional dependency must fail loudly and helpfully
# ==========================================================================

def test_missing_redis_package_raises_an_actionable_import_error():
    real = sys.modules.pop("redis", None)
    real_async = sys.modules.pop("redis.asyncio", None)
    sys.modules["redis"] = None          # force ImportError on import
    try:
        with pytest.raises(ImportError) as exc:
            RedisWAL(url="redis://localhost:6379/0")
        assert "pip install agent-saga[redis]" in str(exc.value)
    finally:
        sys.modules.pop("redis", None)
        if real is not None:
            sys.modules["redis"] = real
        if real_async is not None:
            sys.modules["redis.asyncio"] = real_async


def test_importing_the_wal_package_does_not_require_redis():
    """The core stays dependency-free: RedisWAL is resolved lazily via
    module __getattr__, so `import agent_saga` never touches `redis`."""
    import agent_saga.wal as walpkg

    assert "RedisWAL" in walpkg.__all__
    with pytest.raises(AttributeError):
        walpkg.NoSuchBackend


# ==========================================================================
# Barrier timeout -- the silent-hang fix
# ==========================================================================

@aio
async def test_barrier_raises_wal_stalled_instead_of_hanging_forever():
    """A wedged sink must surface as an error, not an infinite await. Hanging
    forever is a silent failure in the same family as a dropped record."""

    class WedgedWAL(FileWAL):
        async def _flush_batch(self, batch):
            await asyncio.sleep(3600)      # never acknowledges

    with tempfile.TemporaryDirectory() as d:
        wal = WedgedWAL(Path(d) / "wal.jsonl", barrier_timeout=0.2)
        await wal.start()
        wal.append("STEP_INTENT", {"saga_id": "s1"})
        try:
            with pytest.raises(WALStalled, match="did not reach durability"):
                await wal.barrier()
        finally:
            # close() is bounded: it cancels a wedged flusher and still releases
            # the handle, so the temp dir can be removed.
            await wal.close()


@aio
async def test_a_stalled_barrier_aborts_the_step_before_the_side_effect():
    """The intent fence runs BEFORE the forward call, so a stalled WAL must stop
    the effect from happening at all -- not discover it afterwards."""
    fired = []

    class WedgedWAL(FileWAL):
        async def _flush_batch(self, batch):
            await asyncio.sleep(3600)

    with tempfile.TemporaryDirectory() as d:
        wal = WedgedWAL(Path(d) / "wal.jsonl", barrier_timeout=0.2)
        await wal.start()
        ctx = SagaContext(wal=wal)
        with pytest.raises(WALStalled):
            await ctx.execute(
                tool="stripe.charge", semantics=C,
                forward=lambda: fired.append("CHARGED"),
                compensate=lambda r: Compensation(fn=lambda: None, handler="h"))
        assert fired == []          # the money never moved
        await wal.close()


@aio
async def test_barrier_timeout_can_be_disabled_explicitly():
    with tempfile.TemporaryDirectory() as d:
        wal = FileWAL(Path(d) / "wal.jsonl", barrier_timeout=None)
        await wal.start()
        try:
            wal.append("E", {"saga_id": "s"})
            await wal.barrier()          # healthy sink: returns immediately
            assert wal.barrier_timeout is None
        finally:
            await wal.close()
