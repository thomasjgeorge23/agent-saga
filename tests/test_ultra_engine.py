"""`tests/test_ultra_engine.py` -- Unit tests for Zero-Tension Ultra Engine (`import agent_saga.ultra`).
"""

import pytest
import agent_saga
import agent_saga.ultra as ultra


def test_ultra_engine_import():
    assert ultra is not None
    engine = agent_saga.auto_shield()
    assert engine.is_active is True
    status = engine.status()
    assert status["active"] is True
    assert "latency_impact" in status
