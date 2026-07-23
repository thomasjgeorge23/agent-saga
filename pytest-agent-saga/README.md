# pytest-agent-saga

Official Pytest plugin for **agent-saga** — bringing deterministic replay verification, chaos injection testing, and WAL assertions to your AI agent test suite.

## Installation

```bash
pip install pytest-agent-saga
```

## Features & Fixtures

- `saga_wal`: Isolated temporary file WAL fixture per test.
- `chaos_runner`: Inject synthetic failures at step boundaries or run mutation testing over compensation handlers.
- `assert_saga_deterministic`: Verify that a saga's forward and compensation execution matches deterministically across test runs.

## Example

```python
import pytest

async def test_order_saga(saga_wal, chaos_runner):
    # Run saga under chaos injection
    result = await chaos_runner.run(my_order_saga, fail_point=2)
    assert result.rolled_back is True
    assert "refund_payment" in result.compensations_executed
```
