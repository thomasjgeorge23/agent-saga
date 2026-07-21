import asyncio
import contextlib
import logging
from typing import Any, Callable, Union
from pathlib import Path

from ..wal.file_wal import FileWAL
from ..wal.base import BaseWAL
from ..recovery import RecoveryDaemon
from ..decorator import set_default_wal, _active_sagas
from ..locks import get_semantic_locks

logger = logging.getLogger("agent_saga.frameworks.fastapi")


@contextlib.asynccontextmanager
async def _saga_lifespan_impl(app: Any, wal_path: Union[str, Path, BaseWAL]):
    # 1. Startup Logic
    if isinstance(wal_path, BaseWAL):
        wal = wal_path
    else:
        wal = FileWAL(wal_path)
        
    await wal.start()
    set_default_wal(wal)
    app.state.saga_wal = wal
    
    # Create the recovery daemon
    daemon = RecoveryDaemon(wal)
    app.state.saga_daemon = daemon
    
    # Spawn background daemon task
    daemon_task = asyncio.create_task(daemon.watch(interval=5.0))
    app.state.saga_daemon_task = daemon_task
    
    logger.info("agent-saga FastAPI lifespan started: daemon task spawned.")
    
    try:
        yield
    finally:
        # 2. Shutdown Logic
        logger.info("agent-saga FastAPI lifespan shutting down...")
        
        # Cancel the daemon task
        if daemon_task and not daemon_task.done():
            daemon_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await daemon_task
                
        # Await pending compensations/rollbacks for currently active sagas
        if _active_sagas:
            logger.info("Awaiting %d active saga(s) to complete rollback/execution...", len(_active_sagas))
            start_time = asyncio.get_running_loop().time()
            timeout = 10.0
            poll_interval = 0.05
            while _active_sagas and (asyncio.get_running_loop().time() - start_time) < timeout:
                await asyncio.sleep(poll_interval)
                
        # Cleanly release any held semantic locks
        locks = get_semantic_locks()
        if hasattr(locks, "_owners"):
            with getattr(locks, "_mutex", contextlib.nullcontext()):
                locks._owners.clear()
        if hasattr(locks, "close"):
            if asyncio.iscoroutinefunction(locks.close):
                await locks.close()
            else:
                locks.close()
                
        # Cleanly close the default WAL
        await wal.close()
        set_default_wal(None)
        logger.info("agent-saga FastAPI lifespan shutdown completed.")


def saga_lifespan(app_or_path: Any, wal_path: Any = None) -> Callable:
    """FastAPI native lifespan plugin for agent-saga.
    
    Can be used in two ways:
    1. Direct async context manager:
       async with saga_lifespan(app, wal_path):
           yield
           
    2. Registered as the FastAPI app's lifespan function:
       app = FastAPI(lifespan=saga_lifespan("path/to/wal.jsonl"))
    """
    if wal_path is not None:
        # Signature: saga_lifespan(app, wal_path)
        return _saga_lifespan_impl(app_or_path, wal_path)
        
    # Signature: saga_lifespan(wal_path) -> returns asynccontextmanager function
    path_val = app_or_path
    
    @contextlib.asynccontextmanager
    async def lifespan(app: Any):
        async with _saga_lifespan_impl(app, path_val):
            yield
            
    return lifespan
