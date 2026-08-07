"""`tests/test_auto_proxy_and_hitl.py` -- Tests for AutoProxy, HITL Manager & SagaMesh2PC.
"""

import pytest
import agent_saga
from agent_saga.auto_proxy import UniversalAutoProxy, auto_proxy
from agent_saga.hitl import HITLManager, get_hitl_manager, human_in_the_loop
from agent_saga.mesh import SagaMesh2PC
from agent_saga.semantics import ActionSemantics


def test_auto_proxy_inference():
    def get_user_profile(user_id: str):
        """Fetch profile from database."""
        return {"id": user_id}

    def delete_database_table(table_name: str):
        """Delete production database table."""
        return "deleted"

    sem1 = UniversalAutoProxy.infer_semantics(get_user_profile)
    assert sem1 == ActionSemantics.REVERSIBLE

    sem2 = UniversalAutoProxy.infer_semantics(delete_database_table)
    assert sem2 == ActionSemantics.IRREVERSIBLE

    wrapped = auto_proxy(get_user_profile)
    assert wrapped.__saga_semantics__ == ActionSemantics.REVERSIBLE
    res = wrapped("user-101")
    assert res == {"id": "user-101"}


def test_hitl_manager():
    hitl = HITLManager()
    tx = hitl.record_orphaned("saga-999", "stripe_refund", "Stripe API 503 Service Unavailable", {"amount": 500})
    assert tx.status == "NEEDS_HUMAN"
    assert len(hitl.pending_orphaned()) == 1

    success = hitl.resolve("saga-999", "stripe_refund", "Refund processed manually via Stripe Dashboard")
    assert success is True
    assert len(hitl.pending_orphaned()) == 0


def test_saga_mesh_2pc():
    pc = SagaMesh2PC("saga-2pc-88", participants=["node-1", "node-2", "node-3"])
    assert pc.prepare() is True
    assert pc.phase == "PREPARED"
    assert pc.commit() is True
    assert pc.phase == "COMMITTED"
