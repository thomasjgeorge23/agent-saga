"""`agent_saga/integrations` -- Official Framework Integration Adapters.

Provides dedicated 1-line middleware & callback handlers for:
- LangChain (`SagaLangChainCallback`, `SagaRunnable`)
- CrewAI (`SagaCrewHook`)
- AutoGen (`SagaAutoGenMiddleware`)
- FastAPI (`SagaFastAPIMiddleware`)

Published & Maintained by: SAGAOPS Enterprise
Founder & Owner: Thomas J George (thomasjgeorge23@gmail.com)
"""

from __future__ import annotations

from .fastapi import SagaFastAPIMiddleware
from .langchain import SagaLangChainCallback, wrap_runnable
from .crewai import SagaCrewHook
from .autogen import SagaAutoGenMiddleware

__all__ = [
    "SagaAutoGenMiddleware",
    "SagaCrewHook",
    "SagaFastAPIMiddleware",
    "SagaLangChainCallback",
    "wrap_runnable",
]
