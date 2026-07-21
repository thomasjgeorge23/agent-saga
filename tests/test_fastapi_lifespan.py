import tempfile
import asyncio
from pathlib import Path
import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from agent_saga import saga, saga_lifespan, current_saga
from agent_saga.decorator import get_default_wal


@pytest.mark.anyio
async def test_fastapi_lifespan_flow():
    with tempfile.TemporaryDirectory() as tmpdir:
        wal_file = Path(tmpdir) / "wal.jsonl"
        
        # Initialize app with lifespan
        app = FastAPI(lifespan=saga_lifespan(wal_file))
        
        @app.get("/test-saga")
        async def run_saga():
            assert get_default_wal() is not None
            
            @saga
            async def my_flow():
                ctx = current_saga()
                assert ctx is not None
                assert ctx.wal == get_default_wal()
                return "ok"
                
            return await my_flow()
            
        import httpx
        transport = httpx.ASGITransport(app=app)
        
        # Explicitly run the lifespan context manager
        async with app.router.lifespan_context(app):
            # Verify startup state
            assert app.state.saga_wal is not None
            assert app.state.saga_daemon is not None
            assert app.state.saga_daemon_task is not None
            assert not app.state.saga_daemon_task.done()
            
            # Perform a request that accesses the saga and default WAL
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/test-saga")
                assert response.status_code == 200
                assert response.json() == "ok"
            
        # Verify shutdown state
        # Allow the loop to cycle once to process cancellations
        await asyncio.sleep(0.01)
        assert app.state.saga_daemon_task.done()
        assert get_default_wal() is None
