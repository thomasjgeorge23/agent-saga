"""`tests/test_numpy_style_api.py` -- Unit tests for NumPy-like saga.guard and saga.array API.
"""

import pytest
import agent_saga as saga


@pytest.mark.anyio
async def test_saga_guard_decorator():
    @saga.guard
    async def sample_func(x: int) -> int:
        return x * 2

    assert callable(sample_func)


def test_saga_array_operations():
    arr = saga.array([1.0, 2.0, 3.0])
    assert len(arr) == 3
    assert arr[0] == 1.0

    arr[0] = 99.0
    assert arr[0] == 99.0

    res = arr.rollback()
    assert res is True
    assert arr[0] == 1.0


def test_saga_compat_exports():
    from agent_saga.compat import wrap_openai, wrap_langchain, wrap_crewai, wrap_fastapi

    assert callable(wrap_openai)
    assert callable(wrap_langchain)
    assert callable(wrap_crewai)
    assert callable(wrap_fastapi)
