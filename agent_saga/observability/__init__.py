"""Correlation-ID observability.

The WAL already stamps `saga_id` on every event. This does the same for *logs*,
so an operator can `grep` one correlation id across a whole rollback — the
forward call, the failure, and every compensation — instead of guessing which
line belongs to which of a thousand concurrent sagas.

It works through the standard library, no dependency and no change to any
existing `logger.…` call. A `contextvars`-scoped saga/step id is attached to
every LogRecord by a `logging.Filter`; because contextvars follow async tasks,
concurrent sagas never bleed ids into each other's log lines. A JSON formatter
is included for shipping into Datadog/Splunk/Loki; the text formatter is for a
human reading a terminal during an incident.

`configure_logging()` is opt-in — importing the library never reconfigures the
root logger or steals output from the host application.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import sys
import time
from typing import Any, Iterator, Optional

_saga_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "agent_saga_saga_id", default=None)
_step_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "agent_saga_step_id", default=None)

LOGGER_NAME = "agent_saga"


# ---------------------------------------------------------------------------
# Correlation-id binding
# ---------------------------------------------------------------------------

def set_saga_id(saga_id: Optional[str]) -> contextvars.Token:
    """Bind the current saga id. Returns a token to pass to reset_saga_id.
    Used across the begin()/finish() boundary, which is why it is a raw
    set/reset rather than a context manager."""
    return _saga_id.set(saga_id)


def reset_saga_id(token: contextvars.Token) -> None:
    _saga_id.reset(token)


def set_step_id(step_id: Optional[str]) -> contextvars.Token:
    return _step_id.set(step_id)


def reset_step_id(token: contextvars.Token) -> None:
    _step_id.reset(token)


@contextlib.contextmanager
def step_scope(step_id: Optional[str]) -> Iterator[None]:
    """Bind a step id for the duration of a block, then restore the prior one —
    so logs between steps don't inherit the last step's id."""
    token = _step_id.set(step_id)
    try:
        yield
    finally:
        _step_id.reset(token)


def current_correlation() -> tuple[Optional[str], Optional[str]]:
    """The active (saga_id, step_id), for code that wants to propagate them into
    its own telemetry (spans, metrics tags)."""
    return _saga_id.get(), _step_id.get()


# ---------------------------------------------------------------------------
# Logging plumbing
# ---------------------------------------------------------------------------

class CorrelationFilter(logging.Filter):
    """Stamps saga_id / step_id onto every record from the contextvars. A filter
    rather than an adapter so it applies to *every* logger in the package
    without wrapping a single call site."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.saga_id = _saga_id.get() or "-"
        record.step_id = _step_id.get() or "-"
        # When OTel is active, stamp the trace ids too, so a log line and a span
        # can be joined in either direction. No-op (and no import cost) when
        # tracing is off, which is the default.
        from .otel import get_tracer

        trace_id, span_id = get_tracer().correlation()
        record.trace_id = trace_id or "-"
        record.span_id = span_id or "-"
        return True


class TextFormatter(logging.Formatter):
    """Human-readable incident line: level, logger, correlation ids, message."""

    default_time_format = "%H:%M:%S"

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "saga_id"):
            record.saga_id = "-"
        if not hasattr(record, "step_id"):
            record.step_id = "-"
        base = (f"{self.formatTime(record, self.default_time_format)} "
                f"{record.levelname:<7} {record.name} "
                f"[saga={record.saga_id} step={record.step_id}] {record.getMessage()}")
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


# Record attributes that are logging internals, not structured fields to emit.
_RESERVED = frozenset(vars(logging.makeLogRecord({})).keys()) | {
    "message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    """One JSON object per line, ready for a log pipeline. Includes the
    correlation ids and any structured `extra=` fields passed to the logger."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": round(record.created, 6),
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "saga_id": getattr(record, "saga_id", "-"),
            "step_id": getattr(record, "step_id", "-"),
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key not in payload:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(
    *,
    level: int | str = logging.INFO,
    json: bool = False,
    stream: Any = None,
) -> logging.Logger:
    """Attach a correlation-aware handler to the `agent_saga` logger.

    Opt-in and idempotent: it replaces only the handler it previously installed,
    leaves other handlers alone, and does not touch the root logger. Call once at
    startup. `json=True` emits line-delimited JSON for a log pipeline; otherwise
    a readable text line.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    # Do not double-log through the root logger's handlers.
    logger.propagate = False

    for existing in list(logger.handlers):
        if getattr(existing, "_agent_saga_handler", False):
            logger.removeHandler(existing)

    handler = logging.StreamHandler(stream or sys.stderr)
    handler._agent_saga_handler = True  # type: ignore[attr-defined]
    handler.setLevel(level)
    handler.addFilter(CorrelationFilter())
    handler.setFormatter(JsonFormatter() if json else TextFormatter())
    logger.addHandler(handler)
    return logger


__all__ = [
    "configure_logging",
    "CorrelationFilter",
    "JsonFormatter",
    "TextFormatter",
    "set_saga_id",
    "reset_saga_id",
    "step_scope",
    "current_correlation",
    "LOGGER_NAME",
    "link_llm_trace",
]


def __getattr__(name):
    """Expose the OTel surface without importing opentelemetry at import time."""
    if name in ("SagaTracer", "setup_telemetry", "get_tracer", "NoOpTracer", "link_llm_trace"):
        from . import otel

        return getattr(otel, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

