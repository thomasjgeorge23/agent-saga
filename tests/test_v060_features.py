import pytest
import asyncio
from agent_saga.mcp import MCPTransactionProxy
from agent_saga.patch import auto_patch
from agent_saga.pydantic import PydanticSagaAdapter


def test_auto_patch():
    report = auto_patch()
    assert isinstance(report, dict)
    assert "langgraph" in report
    assert "fastapi" in report


def test_mcp_transaction_proxy():
    proxy = MCPTransactionProxy(server_name="test_mcp")

    def mock_charge(amount: float):
        return {"charged": amount}

    def mock_refund(amount: float):
        return {"refunded": amount}

    proxy.register_tool(
        name="charge_user",
        handler=mock_charge,
        inverse_handler=mock_refund,
        semantics="COMPENSABLE",
        description="Charge credit card",
    )

    tools = proxy.list_mcp_tools()
    assert len(tools) == 1
    assert tools[0]["name"] == "charge_user"

    res = asyncio.run(proxy.execute_mcp_call("charge_user", {"amount": 50.0}))
    assert res["status"] == "success"
    assert res["result"]["charged"] == 50.0


def test_pydantic_adapter():
    class DummyModel:
        @classmethod
        def model_json_schema(cls):
            return {"type": "object", "properties": {"item": {"type": "string"}}}

    schema = PydanticSagaAdapter.extract_schema(DummyModel)
    assert schema["type"] == "object"
    assert "item" in schema["properties"]
