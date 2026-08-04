"""`tests/test_omni_engine.py` -- Unit tests for Omnipresent Reality Engine (`saga.omni`).
"""

import pytest
import agent_saga as saga


def test_omni_shield_activation():
    engine = saga.shield()
    assert engine is not None
    assert isinstance(engine, saga.omni.OmniRealityEngine)


@pytest.mark.anyio
async def test_omni_protected_execution_safe():
    engine = saga.shield()

    async def safe_action(value: int) -> int:
        return value * 10

    res = await engine.execute_protected(safe_action, 5)
    assert res["status"] == "COMMITTED_WITH_REALITY_PROOF"
    assert res["result"] == 50
    assert res["certificate"]["verified_safe"] is True


@pytest.mark.anyio
async def test_omni_protected_execution_entropy_healing():
    engine = saga.shield()

    def drop_database():
        return "DELETED"

    res = await engine.execute_protected(drop_database)
    assert res["status"] == "HEALED_AND_PREVENTED"
    assert res["certificate"]["verified_safe"] is False
    assert res["details"]["healed"] is True
