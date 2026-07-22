"""Incremental compensation tracker for streaming tool calls (chunked writes, streaming LLM outputs)."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass
class StreamChunk:
    chunk_index: int
    data: Any
    timestamp: float


class IncrementalCompensationTracker:
    """Tracks partial deltas during streaming tool executions so that aborted or
    interrupted streams can perform incremental unwinding of partial writes."""

    def __init__(self, step_name: str, undo_fn: Optional[Callable[[list[StreamChunk]], Any]] = None):
        self.step_name = step_name
        self.undo_fn = undo_fn
        self.chunks: list[StreamChunk] = []
        self._seq = 0

    def record_chunk(self, data: Any) -> StreamChunk:
        self._seq += 1
        now = time.time()
        try:
            loop = asyncio.get_running_loop()
            now = loop.time()
        except RuntimeError:
            pass
        chunk = StreamChunk(chunk_index=self._seq, data=data, timestamp=now)
        self.chunks.append(chunk)
        return chunk

    async def unwind_partial(self) -> Any:
        """Unwind intermediate partial chunks recorded so far."""
        if not self.chunks:
            return None
        if self.undo_fn is not None:
            if asyncio.iscoroutinefunction(self.undo_fn):
                return await self.undo_fn(list(self.chunks))
            return self.undo_fn(list(self.chunks))
        return len(self.chunks)


def streaming_step(step_name: str, undo_fn: Optional[Callable[[list[StreamChunk]], Any]] = None):
    """Decorator wrapping a streaming tool execution to record partial chunks."""
    def decorator(fn):
        async def wrapper(*args, **kwargs):
            tracker = IncrementalCompensationTracker(step_name=step_name, undo_fn=undo_fn)
            try:
                if asyncio.iscoroutinefunction(fn):
                    res = await fn(tracker, *args, **kwargs)
                else:
                    res = fn(tracker, *args, **kwargs)
                return res
            except Exception as exc:
                await tracker.unwind_partial()
                raise exc
        return wrapper
    return decorator


__all__ = ["StreamChunk", "IncrementalCompensationTracker", "streaming_step"]
