import argparse
from conftest import aio
from agent_saga.connectors import GitHubConnector, CloudConnector, MessagingConnector
from agent_saga.gate import GateContext, DynamicRiskEvaluator, dynamic_risk_rule, Verdict
from agent_saga.semantics import ActionSemantics
from agent_saga.approvals import PostgresApprovalStore, ApprovalRequest
from agent_saga.cli import _cmd_replay


@aio
async def test_github_connector_compensation():
    gh = GitHubConnector()
    pr = await gh.create_pull_request(repo="acme/service", title="Fix bug", head="feature-1")
    assert pr["pr_number"] == 101

    issue = await gh.create_issue(repo="acme/service", title="Issue title", body="Issue text")
    assert issue["issue_number"] == 202

    commit = await gh.commit_file(repo="acme/service", path="src/main.py", content="code", message="patch")
    assert commit["commit_sha"] == "a1b2c3d4e5f67890"


@aio
async def test_cloud_connector_compensation():
    cloud = CloudConnector()
    vm = await cloud.provision_instance(instance_type="t3.micro", region="us-west-2")
    assert vm["instance_id"] == "i-09f8e7d6c5b4a3210"

    bucket = await cloud.create_s3_bucket(bucket_name="my-app-data")
    assert bucket["status"] == "created"

    pod = await cloud.deploy_k8s_pod(pod_name="api-worker", image="nginx:latest")
    assert pod["status"] == "running"


@aio
async def test_messaging_connector_compensation():
    msg = MessagingConnector()
    slack = await msg.post_slack_message(channel="#deploys", text="Deployment starting...")
    assert slack["ts"] == "1721635200.000100"

    discord = await msg.post_discord_message(channel_id="channel_99", text="Hello Discord")
    assert discord["message_id"] == "1234567890987654321"


def test_dynamic_risk_evaluator():
    def custom_scorer(ctx: GateContext) -> float:
        if ctx.kwargs.get("amount", 0) > 1000:
            return 0.95
        return 0.10

    evaluator = DynamicRiskEvaluator(risk_scorer=custom_scorer, risk_threshold=0.70)
    
    ctx_high = GateContext(tool="stripe.charge", semantics=ActionSemantics.COMPENSABLE, kwargs={"amount": 5000})
    score, high_risk = evaluator.evaluate(ctx_high)
    assert score == 0.95
    assert high_risk is True

    rule = dynamic_risk_rule("high-anomaly-rule", evaluator)
    assert rule.when(ctx_high) is True
    assert rule.verdict == Verdict.REQUIRE_APPROVAL


def test_postgres_approval_store():
    store = PostgresApprovalStore()
    req = ApprovalRequest(
        id="req_001", saga_id="saga_001", step_id="step_001",
        tool="stripe.charge", rule="high-amount", reason="spend limit"
    )
    created = store.create(req)
    assert created.id == "req_001"
    assert len(store.pending()) == 1

    decided = store.decide("req_001", granted=True, approver="admin@acme.com")
    assert decided.granted is True
    assert len(store.pending()) == 0


def test_cli_replay_command(tmp_path):
    wal_file = tmp_path / "test.wal"
    wal_file.write_text(
        '{"seq": 1, "saga_id": "saga_test_99", "event": "SAGA_START", "tool": "init", "payload": {}}\n'
        '{"seq": 2, "saga_id": "saga_test_99", "event": "STEP_COMMITTED", "tool": "stripe.charge", "payload": {"amount": 500}}\n'
    )
    args = argparse.Namespace(wal_path=str(wal_file), saga_id="saga_test_99")
    ret = _cmd_replay(args)
    assert ret == 0


def test_link_llm_trace():
    from agent_saga.observability import link_llm_trace
    res = link_llm_trace(saga_id="saga_123", trace_id="tr_456", prompt_context="Refactor stripe billing")
    assert res["saga.id"] == "saga_123"
    assert res["saga.llm_trace_id"] == "tr_456"
    assert res["saga.prompt_context"] == "Refactor stripe billing"

