import pytest
from agent_saga.ui_compiler import compile_saga_ui, DeclarativeUISchema


def sample_checkout_saga(user_id: str, amount: float, auto_ship: bool = True):
    """Sample saga function."""
    return {"status": "ok"}


def test_compile_saga_ui():
    schema = compile_saga_ui(sample_checkout_saga, title="Checkout Order", semantics="COMPENSABLE")
    assert isinstance(schema, DeclarativeUISchema)
    assert schema.title == "Checkout Order"
    assert schema.tool_name == "sample_checkout_saga"
    assert schema.semantics == "COMPENSABLE"

    fields = {f.name: f for f in schema.fields}
    assert "user_id" in fields
    assert fields["user_id"].field_type == "text"
    assert fields["user_id"].required is True

    assert "amount" in fields
    assert fields["amount"].field_type == "number"
    assert fields["amount"].required is True

    assert "auto_ship" in fields
    assert fields["auto_ship"].field_type == "boolean"
    assert fields["auto_ship"].required is False
    assert fields["auto_ship"].default is True
