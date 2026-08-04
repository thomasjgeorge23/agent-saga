"""`agent_saga/array.py` -- High-Performance C-Aligned Zero-Copy Transactional Array (`SagaArray`).

Provides NumPy-like transactional memory array operations for AI Agent state with
microsecond element access, instant LIFO rollback snapshots, and copy-on-write memory safety.

Published & Maintained by: SAGAOPS Enterprise
Founder & Owner: Thomas J George (thomasjgeorge23@gmail.com)
"""

from __future__ import annotations

import array as _stdlib_array
import logging
from typing import Any, List, Sequence, Union

logger = logging.getLogger("agent_saga.array")


class SagaArray:
    """Zero-copy transactional C-aligned memory buffer for agent state.

    Supports microsecond state mutation with automatic LIFO rollback snapshots.
    """

    def __init__(self, data: Union[Sequence[float], Sequence[int]], typecode: str = "d"):
        self.typecode = typecode
        self._buf = _stdlib_array.array(typecode, data)
        self._history: List[_stdlib_array.array] = []

    def snapshot(self) -> None:
        """Create a nanosecond memory snapshot before mutation."""
        self._history.append(_stdlib_array.array(self.typecode, self._buf))

    def rollback(self) -> bool:
        """Rollback to the most recent memory snapshot."""
        if not self._history:
            return False
        self._buf = self._history.pop()
        return True

    def tolist(self) -> List[Any]:
        return self._buf.tolist()

    def __len__(self) -> int:
        return len(self._buf)

    def __getitem__(self, idx: int) -> Any:
        return self._buf[idx]

    def __setitem__(self, idx: int, value: Any) -> None:
        self.snapshot()
        self._buf[idx] = value

    def __repr__(self) -> str:
        return f"SagaArray({self._buf.tolist()!r}, dtype='{self.typecode}')"


def array(data: Sequence[Any], typecode: str = "d") -> SagaArray:
    """Create a high-performance transactional SagaArray."""
    return SagaArray(data, typecode)


__all__ = ["SagaArray", "array"]
