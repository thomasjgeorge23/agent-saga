"""Saga countermeasures for the isolation a saga does not have.

A saga is not a transaction. Each step commits as it runs, so intermediate
state is visible to everyone else immediately -- a second reader can see a
balance the first saga has already spent but not yet confirmed. These are the
standard structural answers to that: tentative state, and semantic locks.
"""

from .tentative import (
    TentativeConflictError,
    TentativeResource,
    TentativeStatus,
    tentative,
)

__all__ = [
    "TentativeStatus",
    "TentativeResource",
    "TentativeConflictError",
    "tentative",
]
