"""OpenTelemetry spans for saga lifecycles, steps and compensations.

The no-op fallback is tested first and unconditionally, because it is the path
every user without the extra installed actually runs.
"""

import logging
import sys
import tempfile
from pathlib import Path

import pytest

from agent_saga import ActionSemantics, AsyncWAL, Compensation, SagaContext, saga
from agent_saga.observability.otel import (
    ATTR_IS_COMPENSATION,
    ATTR_SAGA_ID,
    ATTR_SAGA_STATUS,
    ATTR_STEP_ID,
    SPAN_SAGA,
    NoOpTracer,
    disable_telemetry,
    get_tracer,
    setup_telemetry,
)
from conftest import aio

C = ActionSemantics.COMPENSABLE


@pytest.fixture(autouse=True)
def _telemetry_off():
    """Tracing is opt-in; every test starts from the default and restores it."""
    disable_telemetry()
    yield
    disable_telemetry()


# ==========================================================================
# The zero-dependency contract
# ==========================================================================

def test_the_default_tracer_is_a_noop():
    assert isinstance(get_tracer(), NoOpTracer)
    assert get_tracer().enabled is False


def test_noop_spans_are_usable_context_managers():
    """Null object, not None -- so instrumentation sites need no `if tracer:`
    guard and the traced and untraced paths cannot diverge."""
    with get_tracer().span("anything", {"a": 1}) as span:
        span.set_attribute("k", "v")
        span.record_exception(ValueError("x"))
        span.add_event("e")
        assert span.is_recording() is False


@aio
async def test_a_saga_runs_normally_with_telemetry_disabled():
    log = []
    with tempfile.TemporaryDirectory() as d:
        wal = AsyncWAL(Path(d) / "wal.jsonl")
        await wal.start()
        ctx = SagaContext(wal=wal)
        await ctx.begin()
        await ctx.execute(tool="t", semantics=C, forward=lambda: {"id": 1},
                          compensate=lambda r: Compensation(
                              fn=lambda: log.append("undo"), handler="h"))
        report = await ctx.rollback()
        await ctx.finish(aborted=True, clean=report.clean)
        await wal.close()
    assert log == ["undo"]


def test_setup_without_opentelemetry_falls_back_silently():
    """A missing observability dependency must never take down the transaction
    engine that depends on it."""
    saved = {name: sys.modules.get(name)
             for name in ("opentelemetry", "opentelemetry.trace")}
    sys.modules["opentelemetry"] = None      # force ImportError
    try:
        tracer = setup_telemetry()           # must not raise
        assert isinstance(tracer, NoOpTracer)
    finally:
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
        disable_telemetry()


# ==========================================================================
# Real spans (skipped without the SDK)
# ==========================================================================

def _exporter():
    """A tracer provider wired to an in-memory exporter."""
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    setup_telemetry(provider)
    return exporter


def _by_name(exporter):
    return {s.name: s for s in exporter.get_finished_spans()}


@aio
async def test_a_successful_saga_emits_a_root_span_and_child_step_spans():
    exporter = _exporter()
    with tempfile.TemporaryDirectory() as d:
        wal = AsyncWAL(Path(d) / "wal.jsonl")
        await wal.start()
        ctx = SagaContext(wal=wal)
        await ctx.begin()
        for tool in ("stripe.charge", "postgres.update_row"):
            await ctx.execute(tool=tool, semantics=C, forward=lambda: {"id": 1},
                              compensate=lambda r: Compensation(fn=lambda: None,
                                                                handler="h"))
        await ctx.finish()
        await wal.close()
        saga_id = ctx.saga_id

    spans = _by_name(exporter)
    assert SPAN_SAGA in spans
    assert "saga.step.stripe.charge" in spans
    assert "saga.step.postgres.update_row" in spans

    root = spans[SPAN_SAGA]
    assert root.attributes[ATTR_SAGA_ID] == saga_id
    assert root.attributes[ATTR_SAGA_STATUS] == "COMPLETED"

    step = spans["saga.step.stripe.charge"]
    assert step.attributes[ATTR_IS_COMPENSATION] is False
    assert step.attributes[ATTR_STEP_ID]
    # The step really is a child of the saga, so a trace view nests correctly.
    assert step.parent is not None
    assert step.parent.span_id == root.context.span_id


@aio
async def test_a_failing_saga_emits_rollback_spans_marked_as_compensations():
    exporter = _exporter()

    @saga(reraise=False)
    async def failing():
        from agent_saga import current_saga
        ctx = current_saga()
        await ctx.execute(tool="stripe.charge", semantics=C,
                          forward=lambda: {"id": "ch_1"},
                          compensate=lambda r: Compensation(fn=lambda: None,
                                                            handler="h"))
        raise ValueError("model hallucinated")

    report = await failing()
    assert report.clean

    spans = _by_name(exporter)
    assert "saga.rollback.stripe.charge" in spans
    rollback = spans["saga.rollback.stripe.charge"]
    assert rollback.attributes[ATTR_IS_COMPENSATION] is True
    assert rollback.attributes[ATTR_STEP_ID]
    assert spans[SPAN_SAGA].attributes[ATTR_SAGA_STATUS] == "ROLLED_BACK"


@aio
async def test_a_failing_step_records_the_exception_and_an_error_status():
    from opentelemetry.trace import StatusCode

    exporter = _exporter()
    with tempfile.TemporaryDirectory() as d:
        wal = AsyncWAL(Path(d) / "wal.jsonl")
        await wal.start()
        ctx = SagaContext(wal=wal)
        await ctx.begin()

        def boom():
            raise RuntimeError("stripe 503")

        with pytest.raises(RuntimeError):
            await ctx.execute(tool="stripe.charge", semantics=C, forward=boom,
                              compensate=lambda r: Compensation(fn=lambda: None,
                                                                handler="h"))
        await ctx.finish(aborted=True, clean=False)
        await wal.close()

    step = _by_name(exporter)["saga.step.stripe.charge"]
    assert step.status.status_code is StatusCode.ERROR
    assert any(e.name == "exception" for e in step.events)
    # An incomplete rollback is FAILED, not ROLLED_BACK -- the distinction is
    # the difference between "we cleaned up" and "we tried".
    assert _by_name(exporter)[SPAN_SAGA].attributes[ATTR_SAGA_STATUS] == "FAILED"


# ==========================================================================
# Shared correlation between logs and traces
# ==========================================================================

@aio
async def test_log_records_carry_the_active_trace_and_span_ids():
    """The point of running both: a log line and a span can be joined in either
    direction."""
    import io

    _exporter()
    from agent_saga.observability import CorrelationFilter

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(CorrelationFilter())
    handler.setFormatter(logging.Formatter(
        "%(saga_id)s|%(trace_id)s|%(span_id)s|%(message)s"))
    lg = logging.getLogger("agent_saga.test_otel_corr")
    lg.handlers = [handler]
    lg.setLevel(logging.INFO)
    lg.propagate = False

    with get_tracer().span("probe", {}):
        lg.info("inside a span")

    saga_id, trace_id, span_id, _ = stream.getvalue().strip().split("|", 3)
    assert trace_id != "-" and len(trace_id) == 32
    assert span_id != "-" and len(span_id) == 16


def test_correlation_is_dashes_when_no_span_is_active():
    assert get_tracer().correlation() == (None, None)
