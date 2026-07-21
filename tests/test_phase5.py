import datetime
import uuid
import pytest
import sys
from unittest.mock import MagicMock, AsyncMock, patch
from types import ModuleType

# Setup SQLAlchemy mock module before import
mock_sqlalchemy = ModuleType("sqlalchemy")
mock_sqlalchemy.inspect = MagicMock()
mock_sqlalchemy.select = MagicMock(side_effect=lambda *args: MagicMock())
mock_sqlalchemy.update = MagicMock(side_effect=lambda *args: MagicMock())
mock_sqlalchemy.delete = MagicMock(side_effect=lambda *args: MagicMock())
sys.modules["sqlalchemy"] = mock_sqlalchemy

from pydantic import BaseModel
from agent_saga.serialization import dumps, loads
from agent_saga.wal.file_wal import FileWAL
from agent_saga.adapters.sqlalchemy import SQLAlchemyAdapter
from agent_saga.adapters.supabase import SupabaseAdapter
from agent_saga.decorator import current_saga

# 1. Pydantic & Custom Type Round-Trip Tests

class MyPydanticModel(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime.datetime

@pytest.mark.anyio
async def test_serialization_roundtrip():
    import tempfile
    from pathlib import Path
    
    model_id = uuid.uuid4()
    now = datetime.datetime.now()
    model = MyPydanticModel(id=model_id, name="Test Saga", created_at=now)
    
    with tempfile.TemporaryDirectory() as tmpdir:
        wal = FileWAL(Path(tmpdir) / "wal.jsonl")
        await wal.start()
        
        payload = {
            "model": model,
            "uuid": model_id,
            "datetime": now,
            "tags": {"tag1", "tag2"}
        }
        
        wal.append("TEST_EVENT", payload)
        await wal.barrier()
        
        records = await wal.read_all()
        await wal.close()
        
        assert len(records) == 1
        record = records[0]
        
        # Verify types and values reconstructed perfectly
        assert isinstance(record["model"], MyPydanticModel)
        assert record["model"].id == model_id
        assert record["model"].name == "Test Saga"
        assert record["model"].created_at == now
        assert record["uuid"] == model_id
        assert record["datetime"] == now
        assert record["tags"] == {"tag1", "tag2"}


# 2. SQLAlchemy Adapter Tests

class MockColumn:
    def __init__(self, key):
        self.key = key

class MockMapper:
    def __init__(self, primary_keys, columns):
        self.primary_key = [MockColumn(pk) for pk in primary_keys]
        self.columns = [MockColumn(col) for col in columns]

@pytest.mark.anyio
async def test_sqlalchemy_adapter_insert():
    class MockModel:
        id = object()
        name = object()
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)
                
    mapper = MockMapper(primary_keys=["id"], columns=["id", "name"])
    mock_sqlalchemy.inspect.return_value = mapper
    
    session = AsyncMock()
    session.add = MagicMock()
    adapter = SQLAlchemyAdapter(session)
    
    mock_saga = MagicMock()
    with patch("agent_saga.adapters.base_db.current_saga", return_value=mock_saga):
        res = await adapter.insert(MockModel, {"id": 42, "name": "Alice"})
        
        assert res == {"id": 42, "name": "Alice"}
        session.add.assert_called_once()
        session.flush.assert_called_once()
        
        mock_saga.compensate.assert_called_once()
        handler, *args = mock_saga.compensate.call_args[0]
        assert handler == adapter._delete_rollback
        assert tuple(args) == (MockModel, {"id": 42})


@pytest.mark.anyio
async def test_sqlalchemy_adapter_update():
    class MockModel:
        id = object()
        name = object()
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)
                
    mapper = MockMapper(primary_keys=["id"], columns=["id", "name"])
    mock_sqlalchemy.inspect.return_value = mapper
    
    session = AsyncMock()
    session.add = MagicMock()
    inst = MockModel(id=10, name="Old Name")
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [inst]
    session.execute.return_value = result_mock
    
    adapter = SQLAlchemyAdapter(session)
    mock_saga = MagicMock()
    with patch("agent_saga.adapters.base_db.current_saga", return_value=mock_saga):
        await adapter.update(MockModel, {"name": "New Name"}, {"id": 10})
        
        mock_saga.compensate.assert_called_once()
        handler, *args = mock_saga.compensate.call_args[0]
        assert handler == adapter._update_rollback
        assert tuple(args) == (MockModel, {"name": "Old Name"}, {"id": 10})


@pytest.mark.anyio
async def test_sqlalchemy_adapter_delete():
    class MockModel:
        id = object()
        name = object()
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)
                
    mapper = MockMapper(primary_keys=["id"], columns=["id", "name"])
    mock_sqlalchemy.inspect.return_value = mapper
    
    session = AsyncMock()
    session.add = MagicMock()
    inst = MockModel(id=10, name="To Delete")
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [inst]
    session.execute.return_value = result_mock
    
    adapter = SQLAlchemyAdapter(session)
    mock_saga = MagicMock()
    with patch("agent_saga.adapters.base_db.current_saga", return_value=mock_saga):
        res = await adapter.delete(MockModel, {"id": 10})
        
        assert res == [{"id": 10, "name": "To Delete"}]
        mock_saga.compensate.assert_called_once()
        handler, *args = mock_saga.compensate.call_args[0]
        assert handler == adapter._insert_rollback
        assert tuple(args) == (MockModel, {"id": 10, "name": "To Delete"})


# 3. Supabase Adapter Tests

@pytest.mark.anyio
async def test_supabase_adapter_insert():
    client = MagicMock()
    table_mock = MagicMock()
    client.table.return_value = table_mock
    insert_mock = MagicMock()
    table_mock.insert.return_value = insert_mock
    
    res_mock = MagicMock()
    res_mock.data = [{"id": "usr_123", "name": "Supabase"}]
    insert_mock.execute.return_value = res_mock
    
    adapter = SupabaseAdapter(client)
    mock_saga = MagicMock()
    with patch("agent_saga.adapters.base_db.current_saga", return_value=mock_saga):
        res = await adapter.insert("users", {"name": "Supabase"})
        
        assert res == {"id": "usr_123", "name": "Supabase"}
        mock_saga.compensate.assert_called_once()
        handler, *args = mock_saga.compensate.call_args[0]
        assert handler == adapter._delete_rollback
        assert tuple(args) == ("users", {"id": "usr_123"})


@pytest.mark.anyio
async def test_supabase_adapter_update():
    client = MagicMock()
    table_mock = MagicMock()
    client.table.return_value = table_mock
    
    select_mock = MagicMock()
    table_mock.select.return_value = select_mock
    eq_select = MagicMock()
    select_mock.eq.return_value = eq_select
    res_select = MagicMock()
    res_select.data = [{"id": "usr_123", "name": "Old Supabase"}]
    eq_select.execute.return_value = res_select
    
    update_mock = MagicMock()
    table_mock.update.return_value = update_mock
    eq_update = MagicMock()
    update_mock.eq.return_value = eq_update
    res_update = MagicMock()
    res_update.data = [{"id": "usr_123", "name": "New Supabase"}]
    eq_update.execute.return_value = res_update
    
    adapter = SupabaseAdapter(client)
    mock_saga = MagicMock()
    with patch("agent_saga.adapters.base_db.current_saga", return_value=mock_saga):
        res = await adapter.update("users", {"name": "New Supabase"}, {"id": "usr_123"})
        
        assert res == [{"id": "usr_123", "name": "New Supabase"}]
        mock_saga.compensate.assert_called_once()
        handler, *args = mock_saga.compensate.call_args[0]
        assert handler == adapter._update_rollback
        assert tuple(args) == ("users", {"name": "Old Supabase"}, {"id": "usr_123"})


@pytest.mark.anyio
async def test_supabase_adapter_delete():
    client = MagicMock()
    table_mock = MagicMock()
    client.table.return_value = table_mock
    
    select_mock = MagicMock()
    table_mock.select.return_value = select_mock
    eq_select = MagicMock()
    select_mock.eq.return_value = eq_select
    res_select = MagicMock()
    res_select.data = [{"id": "usr_123", "name": "To Delete"}]
    eq_select.execute.return_value = res_select
    
    delete_mock = MagicMock()
    table_mock.delete.return_value = delete_mock
    eq_delete = MagicMock()
    delete_mock.eq.return_value = eq_delete
    
    adapter = SupabaseAdapter(client)
    mock_saga = MagicMock()
    with patch("agent_saga.adapters.base_db.current_saga", return_value=mock_saga):
        res = await adapter.delete("users", {"id": "usr_123"})
        
        assert res == [{"id": "usr_123", "name": "To Delete"}]
        mock_saga.compensate.assert_called_once()
        handler, *args = mock_saga.compensate.call_args[0]
        assert handler == adapter._insert_rollback
        assert tuple(args) == ("users", {"id": "usr_123", "name": "To Delete"})
