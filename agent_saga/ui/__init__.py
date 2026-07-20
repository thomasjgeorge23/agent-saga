"""Time-travel debugger: a zero-dependency read-only UI over the WAL."""

from .reader import SagaWALReader

__all__ = ["SagaWALReader"]
