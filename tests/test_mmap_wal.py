import tempfile
from pathlib import Path
from agent_saga.wal import MmapWAL
from conftest import aio

@aio
async def test_mmap_wal_lifecycle():
    with tempfile.TemporaryDirectory() as d:
        tmp_path = Path(d)
        wal_path = tmp_path / "mmap_saga_wal.bin"
        wal = MmapWAL(path=wal_path, initial_file_size=1024 * 1024)
        await wal.start()
        try:
            wal.append("STEP_INTENT", {"saga_id": "s1", "tool_name": "deduct_wallet", "args": {"amount": 100}})
            wal.append("STEP_COMPLETED", {"saga_id": "s1", "tool_name": "deduct_wallet", "result": {"status": "ok"}})
            await wal.barrier()

            records = wal.records()
            assert len(records) == 2
            assert records[0]["event"] == "STEP_INTENT"
            assert records[1]["event"] == "STEP_COMPLETED"
        finally:
            await wal.close()

@aio
async def test_mmap_wal_recovery():
    with tempfile.TemporaryDirectory() as d:
        tmp_path = Path(d)
        wal_path = tmp_path / "mmap_recovery_test.bin"
        
        # Process 1: Write records and close
        wal1 = MmapWAL(path=wal_path)
        await wal1.start()
        try:
            wal1.append("STEP_INTENT", {"saga_id": "s2", "tool_name": "create_order"})
            await wal1.barrier()
        finally:
            await wal1.close()

        # Process 2: Re-open WAL and recover existing records
        wal2 = MmapWAL(path=wal_path)
        await wal2.start()
        try:
            records = wal2.records()
            assert len(records) == 1
            assert records[0]["saga_id"] == "s2"
            
            # Append another record in Process 2
            wal2.append("COMPLETED", {"saga_id": "s2"})
            await wal2.barrier()
            
            updated_records = wal2.records()
            assert len(updated_records) == 2
            assert updated_records[1]["event"] == "COMPLETED"
        finally:
            await wal2.close()
