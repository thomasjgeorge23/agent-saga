"""`tests/test_auto_activation.py` -- Unit tests for Zero-Code Auto-Activation (`import agent_saga.auto`).
"""

import pytest
import agent_saga


def test_auto_activation_import():
    import agent_saga.auto
    assert agent_saga.auto is not None
    engine = agent_saga.get_omni_engine()
    assert engine is not None
