"""SagaCloudClient managed control plane (#35): pull_approvals, get_fleet_budget,
get_audit_report, get_fleet_entanglement."""

import tempfile
from conftest import aio

from agent_saga.cloud import SagaCloudClient


def _fake_cloud(url, payload, headers):
    if "/approvals" in url:
        return {"approvals": [
            {"id": "req-1", "status": "GRANTED", "approver": "jane"},
            {"id": "req-2", "status": "pending"},
        ]}
    if "/fleet/budget" in url:
        return {"total": 42000, "by_node": {"node-a": 30000, "node-b": 12000}}
    if "/audit/report" in url:
        return {"report_url": "https://sagaops.dev/reports/abc.csv", "format": "csv"}
    if "/fleet/entanglement" in url:
        return {"nodes": [{"id": "A"}, {"id": "B"}], "edges": [{"source": "B", "target": "A"}]}
    return {"status": "ok"}


@aio
async def test_pull_approvals_applies_to_local_store():
    from agent_saga.approvals import FileApprovalStore, ApprovalRequest
    store = FileApprovalStore(tempfile.mkdtemp())
    store.create(ApprovalRequest(id="req-1", saga_id="s", step_id="x", tool="t", rule="r", reason="z"))

    c = SagaCloudClient(api_key="k", transport=_fake_cloud)
    pulled = await c.pull_approvals(apply_to=store)
    assert len(pulled) == 2
    # the GRANTED cloud decision was applied to the local queue (poll model)
    assert store.get("req-1").status == "GRANTED"


@aio
async def test_get_fleet_budget():
    c = SagaCloudClient(api_key="k", transport=_fake_cloud)
    budget = await c.get_fleet_budget(window=3600)
    assert budget["total"] == 42000 and set(budget["by_node"]) == {"node-a", "node-b"}


@aio
async def test_get_audit_report():
    c = SagaCloudClient(api_key="k", transport=_fake_cloud)
    report = await c.get_audit_report("2026-01-01", "2026-06-30", format="csv")
    assert report["report_url"].endswith(".csv")


@aio
async def test_get_fleet_entanglement():
    c = SagaCloudClient(api_key="k", transport=_fake_cloud)
    graph = await c.get_fleet_entanglement()
    assert len(graph["nodes"]) == 2 and len(graph["edges"]) == 1


@aio
async def test_control_plane_dry_run_sends_nothing():
    def boom(url, payload, headers):
        raise AssertionError("network hit during dry_run")
    c = SagaCloudClient(api_key="k", transport=boom, dry_run=True)
    res = await c.get_fleet_budget()
    assert res["status"] == "dry_run" and res["method"] == "GET" and res["sent"] is False


@aio
async def test_control_plane_degrades_on_failure():
    def down(url, payload, headers):
        raise ConnectionError("cloud unreachable")
    c = SagaCloudClient(api_key="k", transport=down)
    res = await c.get_fleet_entanglement()      # must not raise
    assert res["status"] == "error"
