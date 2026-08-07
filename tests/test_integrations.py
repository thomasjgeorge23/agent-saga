"""`tests/test_integrations.py` -- Unit tests for Official Framework Integration Adapters.
"""

import pytest
import agent_saga
from agent_saga.integrations import (
    SagaAutoGenMiddleware,
    SagaCrewHook,
    SagaFastAPIMiddleware,
    SagaLangChainCallback,
    wrap_runnable,
)


def test_fastapi_middleware():
    class DummyApp:
        async def __call__(self, scope, receive, send):
            pass

    middleware = SagaFastAPIMiddleware(DummyApp())
    assert middleware is not None


def test_langchain_callback():
    cb = SagaLangChainCallback()
    cb.on_llm_start({"name": "gpt-4o"}, ["test prompt"])
    cb.on_tool_start({"name": "search"}, "query")
    cb.on_tool_end("result")
    runnable = wrap_runnable("dummy_runnable")
    assert runnable == "dummy_runnable"


def test_crewai_hook():
    hook = SagaCrewHook()
    hook.on_step("step result")
    assert hook is not None


def test_autogen_middleware():
    mw = SagaAutoGenMiddleware()
    res = mw.process_message("hello")
    assert res == "hello"
