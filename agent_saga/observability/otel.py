"""OpenTelemetry spans for saga lifecycles, steps, and compensations.

A saga is a distributed transaction, and the thing an SRE most wants to see is
the shape of one that went wrong: which step failed, and which compensations ran
because of it. That maps cleanly onto a trace -- a root span per saga, a child
per step, and a child per rollback.

ZERO-DEPENDENCY CONTRACT
    `opentelemetry` is an optional extra. When it is absent, `get_tracer()`
    returns a `NoOpTracer` whose spans are real context managers that do
    nothing. Every instrumentation site therefore has exactly one code path --
    no `if tracer:` guards scattered through the engine, and no behaviour that
    only exists when a dependency happens to be installed.

    Instrumentation is also opt-in even when the library IS installed: nothing
    is traced until `setup_telemetry()` is called. Importing agent_saga must not
    quietly attach to somebody's global tracer provider.

SHARED CORRELATION
    The stdlib logging layer already stamps `saga_id` / `step_id` on every log
    record. When OTel is active, `trace_id` and `span_id` are stamped alongside
    them, so a log line and a span can be joined in either direction -- which is
    the whole reason to run both.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, Iterator, Optional

logger = logging.getLogger("agent_saga.otel")

# Span names, kept as constants so a dashboard query cannot drift from the code.
SPAN_SAGA = "saga.execute"
SPAN_STEP_PREFIX = "saga.step."
SPAN_ROLLBACK_PREFIX = "saga.rollback."

# Attribute keys.
ATTR_SAGA_ID = "saga.id"
ATTR_SAGA_STATUS = "saga.status"
ATTR_STEP_ID = "saga.step_id"
ATTR_IS_COMPENSATION = "saga.is_compensation"
ATTR_SEMANTICS = "saga.semantics"
ATTR_TOOL = "saga.tool"

STATUS_COMPLETED = "COMPLETED"
STATUS_ROLLED_BACK = "ROLLED_BACK"
STATUS_FAILED = "FAILED"


class _NoOpSpan:
    """Satisfies the span surface the engine uses, and does nothing.

    Deliberately not `None`: a null object keeps the call sites unconditional,
    so the traced and untraced paths cannot diverge in behaviour.
    """

    def set_attribute(self, key: str, value: Any) -> None: ...
    def set_attributes(self, attrs: dict) -> None: ...
    def record_exception(self, exc: BaseException) -> None: ...
    def set_status(self, *args: Any, **kwargs: Any) -> None: ...
    def add_event(self, name: str, attributes: Optional[dict] = None) -> None: ...
    def end(self) -> None: ...
    def get_span_context(self) -> None: return None
    def is_recording(self) -> bool: return False


class NoOpTracer:
    """The default. Every span is a no-op context manager."""

    enabled = False

    @contextlib.contextmanager
    def span(self, name: str, attributes: Optional[dict] = None) -> Iterator[_NoOpSpan]:
        yield _NoOpSpan()

    def correlation(self) -> tuple[Optional[str], Optional[str]]:
        return None, None


class SagaTracer:
    """Thin wrapper over an OpenTelemetry tracer.

    Wrapping rather than exposing the OTel tracer directly keeps the engine's
    instrumentation sites free of OTel imports and enum handling, and makes the
    no-op fallback a drop-in.
    """

    enabled = True

    def __init__(self, tracer: Any, trace_module: Any):
        self._tracer = tracer
        self._trace = trace_module

    @contextlib.contextmanager
    def span(self, name: str, attributes: Optional[dict] = None) -> Iterator[Any]:
        with self._tracer.start_as_current_span(name) as span:
            for key, value in (attributes or {}).items():
                if value is not None:
                    span.set_attribute(key, value)
            try:
                yield span
            except BaseException as exc:
                # Record before re-raising: an un-annotated error span is a
                # trace that shows something broke but not what.
                span.record_exception(exc)
                span.set_status(self._trace.Status(
                    self._trace.StatusCode.ERROR, str(exc)))
                raise

    def correlation(self) -> tuple[Optional[str], Optional[str]]:
        """The active trace and span ids, hex-formatted for log correlation."""
        span = self._trace.get_current_span()
        ctx = span.get_span_context() if span else None
        if ctx is None or not getattr(ctx, "is_valid", False):
            return None, None
        return f"{ctx.trace_id:032x}", f"{ctx.span_id:016x}"


_TRACER: Any = NoOpTracer()


def setup_telemetry(tracer_provider: Any = None) -> Any:
    """Turn on tracing. Opt-in, and safe to call when OTel is not installed.

    Returns the active tracer -- a `SagaTracer` on success, a `NoOpTracer` if
    `opentelemetry` is missing. It does not raise: an observability dependency
    must never be able to take down the transaction engine that depends on it.
    """
    global _TRACER
    try:
        from opentelemetry import trace as ot_trace
    except ImportError:
        logger.info(
            "OpenTelemetry is not installed; tracing stays disabled. "
            "pip install agent-saga[opentelemetry] to enable it.")
        _TRACER = NoOpTracer()
        return _TRACER

    provider = tracer_provider or ot_trace.get_tracer_provider()
    _TRACER = SagaTracer(provider.get_tracer("agent_saga"), ot_trace)
    logger.info("OpenTelemetry tracing enabled for agent_saga")
    return _TRACER


def disable_telemetry() -> None:
    global _TRACER
    _TRACER = NoOpTracer()


def get_tracer() -> Any:
    return _TRACER


def step_span_name(tool: str) -> str:
    return f"{SPAN_STEP_PREFIX}{tool}"


def rollback_span_name(tool: str) -> str:
    return f"{SPAN_ROLLBACK_PREFIX}{tool}"


def link_llm_trace(saga_id: str, trace_id: str, prompt_context: Optional[str] = None, hallucination_score: float = 0.0) -> dict[str, Any]:
    """Binds an LLM prompt trace (LangSmith, Phoenix, OpenTelemetry) directly to a Saga UUID.

    When a transaction fails or rolls back, this association exposes the exact
    hallucinated prompt that triggered the failure.
    """
    payload = {
        ATTR_SAGA_ID: saga_id,
        "saga.llm_trace_id": trace_id,
        "saga.prompt_context": prompt_context or "",
        "saga.hallucination_score": hallucination_score,
    }
    tracer = get_tracer()
    if getattr(tracer, "enabled", False):
        tracer.span("saga.llm_trace", attributes=payload)
    logger.info("Linked Saga %s to LLM Trace %s (prompt: %s)", saga_id[:12], trace_id, (prompt_context or "")[:40])
    return payload


__all__ = [
    "SagaTracer", "NoOpTracer", "setup_telemetry", "disable_telemetry",
    "get_tracer", "step_span_name", "rollback_span_name", "link_llm_trace",
    "SPAN_SAGA", "SPAN_STEP_PREFIX", "SPAN_ROLLBACK_PREFIX",
    "ATTR_SAGA_ID", "ATTR_SAGA_STATUS", "ATTR_STEP_ID", "ATTR_IS_COMPENSATION",
    "ATTR_SEMANTICS", "ATTR_TOOL",
    "STATUS_COMPLETED", "STATUS_ROLLED_BACK", "STATUS_FAILED",
]

