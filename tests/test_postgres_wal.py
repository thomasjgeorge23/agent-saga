import pytest
import asyncio
import json
import sys
from typing import List, Dict, Any
from agent_saga.wal.postgres_wal import PostgresWAL
from agent_saga import ActionSemantics, BaseWAL

C = ActionSemantics.COMPENSABLE


class FakeConnection:
    def __init__(self):
        self.executed_queries = []
        self.executemany_calls = []
        self.fetched_rows = []

    async def execute(self, query, *args):
        self.executed_queries.append((query, args))
        return "OK"

    async def executemany(self, query, args_list):
        self.executemany_calls.append((query, args_list))
        return "INSERT"

    async def fetch(self, query, *args):
        return self.fetched_rows


class FakePool:
    def __init__(self, connection: FakeConnection):
        self.conn = connection
        self.closed = False

    def acquire(self):
        class AsyncContextManager:
            def __init__(self, conn):
                self.conn = conn
            async def __aenter__(self):
                return self.conn
            async def __aexit__(self, exc_type, exc, tb):
                pass
        return AsyncContextManager(self.conn)

    async def close(self):
        self.closed = True


@pytest.mark.anyio
async def test_postgres_wal_creates_table_on_start():
    conn = FakeConnection()
    pool = FakePool(conn)
    wal = PostgresWAL(pool=pool, table_name="custom_wal_table")
    
    await wal.start()
    try:
        assert len(conn.executed_queries) == 1
        create_query = conn.executed_queries[0][0]
        assert "CREATE TABLE IF NOT EXISTS custom_wal_table" in create_query
        assert "id SERIAL PRIMARY KEY" in create_query
        assert "saga_id TEXT" in create_query
        assert "step_id TEXT" in create_query
        assert "payload JSONB" in create_query
    finally:
        await wal.close()
    assert pool.closed is False


@pytest.mark.anyio
async def test_postgres_wal_append_executes_bulk_insert():
    conn = FakeConnection()
    pool = FakePool(conn)
    wal = PostgresWAL(pool=pool)
    
    await wal.start()
    try:
        wal.append("SAGA_START", {"saga_id": "saga-123"})
        wal.append("STEP_COMMITTED", {"saga_id": "saga-123", "step_id": "step-999", "tool": "test"})
        
        await wal.barrier()
        
        assert len(conn.executemany_calls) == 1
        query, args_list = conn.executemany_calls[0]
        assert "INSERT INTO saga_wal" in query
        assert len(args_list) == 2
        
        # Verify first insert row params
        assert args_list[0][0] == "saga-123"
        assert args_list[0][1] is None  # no step_id on SAGA_START
        payload_1 = json.loads(args_list[0][2])
        data_1 = json.loads(payload_1["data"])
        assert data_1["event"] == "SAGA_START"
        assert data_1["saga_id"] == "saga-123"

        # Verify second insert row params
        assert args_list[1][0] == "saga-123"
        assert args_list[1][1] == "step-999"
        payload_2 = json.loads(args_list[1][2])
        data_2 = json.loads(payload_2["data"])
        assert data_2["event"] == "STEP_COMMITTED"
        assert data_2["step_id"] == "step-999"
        assert data_2["tool"] == "test"
    finally:
        await wal.close()


@pytest.mark.anyio
async def test_postgres_wal_read_all_deserializes_correctly():
    conn = FakeConnection()
    pool = FakePool(conn)
    wal = PostgresWAL(pool=pool)
    
    record_1 = {"seq": 1, "event": "SAGA_START", "saga_id": "saga-1"}
    record_2 = {"seq": 2, "event": "STEP_COMMITTED", "saga_id": "saga-1", "step_id": "step-1", "tool": "test_tool"}
    
    # Mock database rows returned
    conn.fetched_rows = [
        {"payload": json.dumps({"data": json.dumps(record_1)})},
        {"payload": json.dumps({"data": json.dumps(record_2)})},
    ]
    
    await wal.start()
    try:
        records = await wal.read_all()
        assert len(records) == 2
        assert records[0]["event"] == "SAGA_START"
        assert records[0]["saga_id"] == "saga-1"
        assert records[1]["event"] == "STEP_COMMITTED"
        assert records[1]["step_id"] == "step-1"
        assert records[1]["tool"] == "test_tool"
    finally:
        await wal.close()


@pytest.mark.anyio
async def test_postgres_wal_clear_deletes_records():
    conn = FakeConnection()
    pool = FakePool(conn)
    wal = PostgresWAL(pool=pool)
    
    await wal.start()
    try:
        await wal.clear()
        assert len(conn.executed_queries) == 2  # create table + delete query
        delete_query = conn.executed_queries[1][0]
        assert "DELETE FROM saga_wal" in delete_query
    finally:
        await wal.close()


def test_missing_asyncpg_raises_import_error():
    # Simulate asyncpg not installed
    real_asyncpg = sys.modules.pop("asyncpg", None)
    sys.modules["asyncpg"] = None
    try:
        with pytest.raises(ImportError) as exc_info:
            PostgresWAL(pool=None)
        assert "pip install agent-saga[postgres]" in str(exc_info.value)
    finally:
        sys.modules.pop("asyncpg", None)
        if real_asyncpg is not None:
            sys.modules["asyncpg"] = real_asyncpg
