"""Correlation-id observability: saga_id/step_id threaded through logs, JSON
formatting for pipelines, and a clean rollback diagnostic trace."""

import io
import json
import logging

import pytest

from agent_saga import (
    ActionSemantics,
    AsyncWAL,
    Compensation,
    SagaContext,
    configure_logging,
    current_correlation,
    saga,
)
from agent_saga.observability import (
    CorrelationFilter,
    JsonFormatter,
    TextFormatter,
    set_saga_id,
    set_step_id,
    step_scope,
)
from conftest import aio

C = ActionSemantics.COMPENSABLE


def _capturing_logger(formatter):
    """A fresh isolated logger with the correlation filter and given formatter,
    writing to a StringIO we can assert on."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(CorrelationFilter())
    handler.setFormatter(formatter)
    lg = logging.getLogger("agent_saga.test_obs")
    lg.handlers = [handler]
    lg.setLevel(logging.INFO)
    lg.propagate = False
    return lg, stream


# --------------------------------------------------------------------------
# The filter stamps correlation ids from the contextvars
# --------------------------------------------------------------------------

def test_filter_injects_bound_saga_and_step_ids():
    lg, stream = _capturing_logger(TextFormatter())
    tok = set_saga_id("saga-abc")
    try:
        with step_scope("step-9"):
            lg.info("charging card")
    finally:
        from agent_saga.observability import reset_saga_id
        reset_saga_id(tok)
    line = stream.getvalue()
    assert "saga=saga-abc" in line and "step=step-9" in line and "charging card" in line


def test_filter_defaults_to_dash_outside_a_saga():
    lg, stream = _capturing_logger(TextFormatter())
    lg.info("no saga here")
    assert "saga=- step=-" in stream.getvalue()


def test_step_scope_restores_the_previous_step_id():
    set_step_id("outer")
    with step_scope("inner"):
        assert current_correlation()[1] == "inner"
    assert current_correlation()[1] == "outer"
    set_step_id(None)


# --------------------------------------------------------------------------
# JSON formatter for log pipelines
# --------------------------------------------------------------------------

def test_json_formatter_emits_structured_correlated_lines():
    lg, stream = _capturing_logger(JsonFormatter())
    tok = set_saga_id("saga-json")
    try:
        lg.info("refund issued", extra={"charge_id": "ch_1", "amount": 4200})
    finally:
        from agent_saga.observability import reset_saga_id
        reset_saga_id(tok)

    rec = json.loads(stream.getvalue().strip())
    assert rec["saga_id"] == "saga-json"
    assert rec["level"] == "INFO"
    assert rec["message"] == "refund issued"
    assert rec["charge_id"] == "ch_1" and rec["amount"] == 4200
    assert "ts" in rec and "time" in rec


def test_json_formatter_captures_exception_text():
    lg, stream = _capturing_logger(JsonFormatter())
    try:
        raise ValueError("boom")
    except ValueError:
        lg.error("it failed", exc_info=True)
    rec = json.loads(stream.getvalue().strip())
    assert "ValueError: boom" in rec["exc"]


# --------------------------------------------------------------------------
# configure_logging is opt-in and idempotent
# --------------------------------------------------------------------------

def test_configure_logging_is_idempotent():
    stream = io.StringIO()
    configure_logging(stream=stream, level=logging.INFO)
    configure_logging(stream=stream, level=logging.INFO)   # again
    lg = logging.getLogger("agent_saga")
    installed = [h for h in lg.handlers if getattr(h, "_agent_saga_handler", False)]
    assert len(installed) == 1     # not duplicated
    # restore default state so other tests see no stray handler
    for h in installed:
        lg.removeHandler(h)
    lg.propagate = True


# --------------------------------------------------------------------------
# End to end: a real saga's logs carry the correlation id across a rollback
# --------------------------------------------------------------------------

@aio
async def test_saga_logs_are_correlated_across_the_whole_rollback():
    # Capture the package logger during one saga.
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(CorrelationFilter())
    handler.setFormatter(TextFormatter())
    pkg = logging.getLogger("agent_saga")
    prev_handlers, prev_level, prev_prop = pkg.handlers, pkg.level, pkg.propagate
    pkg.handlers = [handler]
    pkg.setLevel(logging.INFO)
    pkg.propagate = False

    try:
        log = []

        @saga(reraise=False)
        async def run():
            from agent_saga import current_saga
            ctx = current_saga()
            await ctx.execute(
                tool="stripe.charge", semantics=C,
                forward=lambda: {"id": "ch_1"},
                compensate=lambda r: Compensation(fn=lambda: log.append("refund"), handler="h"))
            raise ValueError("model hallucinated")

        report = await run()
        assert report.clean and log == ["refund"]
    finally:
        out = stream.getvalue()
        pkg.handlers, pkg.propagate = prev_handlers, prev_prop
        pkg.setLevel(prev_level)

    lines = [l for l in out.splitlines() if l.strip()]
    # Every emitted line carries a concrete saga id (never the bare dash).
    assert lines and all("saga=" in l for l in lines)
    saga_ids = {l.split("saga=")[1].split(" ")[0] for l in lines}
    assert saga_ids != {"-"} and "-" not in saga_ids   # a real id, on every line

    joined = "\n".join(lines)
    assert "saga started" in joined
    assert "rollback triggered by ValueError: model hallucinated" in joined
    assert "compensated 'stripe.charge'" in joined
    assert "rollback complete" in joined
    # The compensation line is correlated to the step, not just the saga.
    comp_line = next(l for l in lines if "compensated 'stripe.charge'" in l)
    assert "step=" in comp_line and "step=-" not in comp_line


@aio
async def test_correlation_ids_do_not_leak_between_concurrent_sagas():
    import asyncio
    from agent_saga import current_saga

    seen = {}

    @saga(reraise=False)
    async def one(name):
        await asyncio.sleep(0.01)
        seen[name] = current_correlation()[0]

    await asyncio.gather(one("a"), one("b"))
    # Each saga saw its own id, and they differ.
    assert seen["a"] and seen["b"] and seen["a"] != seen["b"]
    # After the sagas finish, the ambient context is clean again.
    assert current_correlation() == (None, None)
