"""Retry policy for saga steps.

Validated by hand rather than with pydantic. `agent_saga` declares no runtime
dependencies, and this module sits on the unconditional `import agent_saga`
path -- a `from pydantic import ...` here makes the whole package unimportable
on a clean install, and only appeared to work because langchain-core/crewai
drag pydantic in transitively. Same reason limits.py, gate.py and approvals.py
check their own arguments.
"""

from dataclasses import dataclass, field
from typing import Any, List, Literal, Type

_BACKOFF_TYPES = ("linear", "exponential")


# Constraint failures raise ValueError, not TypeError: pydantic's
# ValidationError subclasses ValueError, so anything already catching a bad
# RetryPolicy keeps catching it.
def _non_negative_int(name: str, value: Any) -> int:
    # bool is an int subclass; RetryPolicy(max_retries=True) is a mistake, and
    # pydantic rejected it too.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"RetryPolicy {name}={value!r} must be an int, got "
            f"{type(value).__name__}."
        )
    if value < 0:
        raise ValueError(
            f"RetryPolicy {name}={value} must be >= 0. Use 0 to disable retries."
        )
    return value


def _positive_float(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"RetryPolicy {name}={value!r} must be a number, got "
            f"{type(value).__name__}."
        )
    if value <= 0:
        raise ValueError(
            f"RetryPolicy {name}={value} must be > 0. A non-positive delay "
            f"would retry a failing step with no pause at all."
        )
    return float(value)


def _exception_list(name: str, value: Any) -> List[Type[BaseException]]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(
            f"RetryPolicy {name} must be a list of exception classes, got "
            f"{type(value).__name__}."
        )
    for item in value:
        # `issubclass` on a non-class raises TypeError, and this list is walked
        # inside the retry loop of a step that is already failing -- reject it
        # at construction instead.
        if not (isinstance(item, type) and issubclass(item, BaseException)):
            raise ValueError(
                f"RetryPolicy {name} entry {item!r} is not an exception class."
            )
    return list(value)


@dataclass
class RetryPolicy:
    max_retries: int = 3
    backoff_type: Literal["linear", "exponential"] = "exponential"
    base_delay: float = 1.0
    max_delay: float = 60.0
    retry_on: List[Type[BaseException]] = field(
        default_factory=lambda: [Exception])
    exclude_exceptions: List[Type[BaseException]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.max_retries = _non_negative_int("max_retries", self.max_retries)
        if self.backoff_type not in _BACKOFF_TYPES:
            raise ValueError(
                f"RetryPolicy backoff_type={self.backoff_type!r} must be one of "
                f"{' or '.join(repr(b) for b in _BACKOFF_TYPES)}."
            )
        # int -> float, matching what pydantic coerced: base_delay=1 stayed 1.0.
        self.base_delay = _positive_float("base_delay", self.base_delay)
        self.max_delay = _positive_float("max_delay", self.max_delay)
        self.retry_on = _exception_list("retry_on", self.retry_on)
        self.exclude_exceptions = _exception_list(
            "exclude_exceptions", self.exclude_exceptions)

    def calculate_delay(self, attempt: int) -> float:
        """Calculate the backoff delay for a given attempt index (0-indexed)."""
        if self.backoff_type == "linear":
            delay = self.base_delay * (attempt + 1)
        else:
            delay = self.base_delay * (2 ** attempt)
        return min(delay, self.max_delay)


__all__ = ["RetryPolicy"]
