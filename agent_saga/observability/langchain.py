"""Zero-friction LLM-trace linking for LangChain / LangSmith.

Drop ``LangChainSagaCallback`` into any LangChain call and every LLM completion
is automatically bound to the current saga via ``link_llm_trace`` -- so when a
transaction later rolls back, the exact prompt/trace that drove it is one hop
away in the WAL and the dashboard, with no manual instrumentation at each call
site.

    from agent_saga.observability import LangChainSagaCallback

    cb = LangChainSagaCallback()                 # saga id pulled from context
    llm.invoke(prompt, config={"callbacks": [cb]})
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger("agent_saga.observability.langchain")


def _base_handler() -> type:
    """Use LangChain's BaseCallbackHandler as the base when it is installed, so
    LangChain recognises this handler; otherwise fall back to ``object`` so the
    class still imports and can be unit-tested without the dependency."""
    try:
        from langchain_core.callbacks import BaseCallbackHandler
        return BaseCallbackHandler
    except ImportError:
        return object


class LangChainSagaCallback(_base_handler()):  # type: ignore[misc]
    """LangChain callback handler that links every LLM completion to a saga.

    ``saga_id`` may be given explicitly; if omitted it is read from the active
    saga context at completion time. ``hallucination_scorer`` is an optional
    callable taking the LLM response and returning a float risk score that is
    attached to the link (default 0.0).
    """

    def __init__(
        self,
        saga_id: Optional[str] = None,
        *,
        hallucination_scorer: Optional[Callable[[Any], float]] = None,
    ):
        # Cooperative init: BaseCallbackHandler.__init__ takes no args, object's
        # is a no-op -- both are safe to call.
        try:
            super().__init__()
        except Exception:
            pass
        self.saga_id = saga_id
        self.hallucination_scorer = hallucination_scorer
        self._prompts: dict[str, str] = {}   # run_id -> joined prompt text
        self.links: list[dict] = []          # every link_llm_trace payload emitted

    # -- LangChain callback surface (flexible kwargs across versions) -------

    def on_llm_start(self, serialized: Any = None, prompts: Any = None, **kwargs: Any) -> None:
        run_id = str(kwargs.get("run_id", ""))
        if run_id and prompts:
            try:
                self._prompts[run_id] = " ".join(str(p) for p in prompts)[:2000]
            except Exception:
                self._prompts[run_id] = str(prompts)[:2000]

    def on_chat_model_start(self, serialized: Any = None, messages: Any = None, **kwargs: Any) -> None:
        # Chat models deliver messages instead of raw prompts; flatten to text.
        run_id = str(kwargs.get("run_id", ""))
        if run_id and messages:
            self._prompts[run_id] = str(messages)[:2000]

    def on_llm_end(self, response: Any = None, **kwargs: Any) -> None:
        run_id = str(kwargs.get("run_id", ""))
        self._link(run_id, response)

    def on_llm_error(self, error: Any = None, **kwargs: Any) -> None:
        run_id = str(kwargs.get("run_id", ""))
        # A failed generation is exactly the case a rollback will want to trace.
        self._link(run_id, None, note=f"llm_error: {error!r}")

    # -- linking -----------------------------------------------------------

    def _resolve_saga_id(self) -> str:
        if self.saga_id:
            return self.saga_id
        try:
            from . import current_correlation
            sid, _ = current_correlation()
            return sid or "unknown"
        except Exception:
            return "unknown"

    def _link(self, run_id: str, response: Any, *, note: str = "") -> None:
        try:
            from .otel import link_llm_trace

            score = 0.0
            if self.hallucination_scorer is not None and response is not None:
                try:
                    score = float(self.hallucination_scorer(response))
                except Exception:
                    logger.debug("hallucination_scorer failed", exc_info=True)
            prompt_ctx = self._prompts.pop(run_id, "") or note
            payload = link_llm_trace(
                saga_id=self._resolve_saga_id(),
                trace_id=run_id or "unknown",
                prompt_context=prompt_ctx,
                hallucination_score=score,
            )
            self.links.append(payload)
        except Exception:
            # Telemetry must never break the LLM call it is observing.
            logger.exception("LangChainSagaCallback failed to link trace")


__all__ = ["LangChainSagaCallback"]
