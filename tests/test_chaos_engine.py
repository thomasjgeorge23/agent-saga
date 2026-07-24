import os
import pytest
import tempfile
from pathlib import Path
from agent_saga import ChaosConfig, ChaosEngine, ChaosInjectionError
from conftest import aio

@aio
async def test_chaos_engine_step_failure():
    chaos = ChaosEngine(ChaosConfig(fail_at_step_index=2))
    
    await chaos.before_step("step_one", {})
    with pytest.raises(ChaosInjectionError, match="Simulated fault at step 2"):
        await chaos.before_step("step_two", {})

@aio
async def test_chaos_engine_tool_name_failure():
    chaos = ChaosEngine(ChaosConfig(fail_at_tool_name="deduct_wallet"))
    
    await chaos.before_step("check_inventory", {})
    with pytest.raises(ChaosInjectionError, match="deduct_wallet"):
        await chaos.before_step("deduct_wallet", {})

def test_chaos_corrupt_wal_bytes():
    with tempfile.NamedTemporaryFile("w+", delete=False) as f:
        f.write("Line 1: Valid WAL Data\nLine 2: Valid WAL Data\n")
        path_str = f.name

    try:
        chaos = ChaosEngine()
        chaos.corrupt_file_bytes(path_str)
        
        with open(path_str, "rb") as f:
            content = f.read()
            assert b"BADBYTES" in content
    finally:
        if os.path.exists(path_str):
            os.remove(path_str)
