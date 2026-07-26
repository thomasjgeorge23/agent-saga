"""Universal Agent Enhancement Engine (`agent_saga.enhance` & `agent_saga.patch_all`).

Transforms any raw LLM response (OpenAI, Anthropic Claude, Google Gemini, Ollama)
into a next-level, fault-tolerant, self-healing, interactive, and auditable AgentKit transaction.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Union

from .agentkit import AgentKit
from .approvals import ApprovalRequest, UIConfirmationArtifact
from .dag import DAGSaga
from .healing import HealingPath, SelfHealingGraph
from .preview import PreviewPlan, PreviewSaga

logger = logging.getLogger("agent_saga.universal")


@dataclass
class EnhancedAIResponse:
    """Next-Level Enhanced AI Response Payload emitted by agent-saga."""

    original_content: str
    preview_plan: Optional[Dict[str, Any]] = None
    self_healed_proposal: Optional[Dict[str, Any]] = None
    ui_confirmation_card: Optional[Dict[str, Any]] = None
    audit_hash: str = ""
    status: str = "SUCCESS"  # SUCCESS, SUSPENDED, HEALED, BLOCKED
    executed_steps: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def format_markdown(self) -> str:
        """Format the response into a rich markdown string suitable for chat UIs."""
        parts = [self.original_content]

        if self.preview_plan:
            parts.append("\n\n### 🔮 Pre-Flight Speculative Plan")
            parts.append(f"```json\n{json.dumps(self.preview_plan, indent=2)}\n```")

        if self.self_healed_proposal:
            parts.append("\n\n### ⚡ Self-Healed Action Proposal")
            parts.append(f"> {self.self_healed_proposal.get('explanation', '')}")

        if self.ui_confirmation_card:
            parts.append("\n\n### ⏸️ Confirmation Required")
            parts.append(f"```json\n{json.dumps(self.ui_confirmation_card, indent=2)}\n```")

        if self.audit_hash:
            parts.append(f"\n\n*🛡️ Audit Signature: `{self.audit_hash[:16]}`*")

        return "\n".join(parts)


def _audit_signature(content: str, executed_steps: List[str]) -> str:
    """A reproducible SHA-256 over the response and the steps it caused.

    Uses the same canonical JSON encoding as the WAL's hash chain, so a third
    party can recompute this from the recorded fields alone.
    """
    import hashlib

    payload = json.dumps(
        {"content": content, "executed_steps": list(executed_steps)},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


class UniversalAgentEngine:
    """Global engine that converts raw LLM outputs into proactive, self-healing, transactional executions."""

    def __init__(self, name: str = "global_universal_agent"):
        self.kit = AgentKit(name=name)
        self.healing_graph = SelfHealingGraph()
        self.name = name

    def register_healing_path(self, primary_tool: str, fallback_tool: str, fallback_fn: Callable[..., Any]) -> None:
        """Register a self-healing fallback path for any tool."""
        self.healing_graph.register_path(HealingPath(
            primary_tool=primary_tool,
            fallback_tool=fallback_tool,
            fallback_fn=fallback_fn,
        ))

    async def execute_enhanced(
        self,
        llm_response_text: str,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        preview_checks: Optional[List[Callable[[], Any]]] = None,
    ) -> EnhancedAIResponse:
        """Transform any LLM output + tool calls into a next-level EnhancedAIResponse."""
        tool_calls = tool_calls or []
        preview_plan_dict = None
        healed_proposal_dict = None
        ui_card_dict = None
        executed_steps = []

        # 1. Speculative Pre-Flight Intelligence
        if preview_checks:
            preview = PreviewSaga()
            for idx, check_fn in enumerate(preview_checks):
                await preview.check(f"preflight_check_{idx+1}", check_fn)
            plan = preview.build_plan()
            preview_plan_dict = plan.__dict__

        # 2. Process Tool Calls inside a Transaction Boundary
        status_label = "SUCCESS"
        if tool_calls:
            async with self.kit.transaction():
                for call in tool_calls:
                    tool_name = call.get("name", "unknown_tool")
                    args = call.get("args", {})
                    fn = call.get("fn")

                    if not fn:
                        continue

                    try:
                        # Wrap tool with safe execution
                        wrapped = self.kit.safe_tool(fn, name=tool_name, semantics="COMPENSABLE")
                        await wrapped(**args)
                        executed_steps.append(tool_name)
                    except Exception as exc:
                        # Attempt Self-Healing
                        healed = await self.healing_graph.try_heal_with_proposal(tool_name, args, exc)
                        if healed:
                            healed_proposal_dict = healed.__dict__
                            status_label = "HEALED"
                            executed_steps.append(f"{tool_name} (healed -> {healed.fallback_tool})")
                        else:
                            status_label = "BLOCKED"
                            raise exc

        # 3. Generate Audit Signature.
        # Real SHA-256 over canonical bytes. The previous implementation used
        # Python's builtin hash(), which is randomised per process (PYTHONHASHSEED)
        # -- so the same response produced a different "signature" on every run,
        # while being labelled sha256_. A signature that cannot be reproduced by
        # anyone else proves nothing.
        audit_hash = _audit_signature(llm_response_text, executed_steps)

        return EnhancedAIResponse(
            original_content=llm_response_text,
            preview_plan=preview_plan_dict,
            self_healed_proposal=healed_proposal_dict,
            ui_confirmation_card=ui_card_dict,
            audit_hash=audit_hash,
            status=status_label,
            executed_steps=executed_steps,
        )


# Global Engine Instance for zero-boilerplate use
_global_engine = UniversalAgentEngine()


async def aenhance(
    llm_response_text: str,
    tool_calls: Optional[List[Dict[str, Any]]] = None,
    preview_checks: Optional[List[Callable[[], Any]]] = None,
) -> EnhancedAIResponse:
    """Enhance an LLM response from inside async code. This is the form agents
    actually want, since an agent loop is already running an event loop."""
    return await _global_engine.execute_enhanced(
        llm_response_text, tool_calls, preview_checks)


def enhance(
    llm_response_text: str,
    tool_calls: Optional[List[Dict[str, Any]]] = None,
    preview_checks: Optional[List[Callable[[], Any]]] = None,
) -> EnhancedAIResponse:
    """Enhance an LLM response from synchronous code.

    Must not be called while an event loop is running in this thread -- blocking
    a running loop is impossible, not merely discouraged. The previous version
    tried ``loop.run_until_complete()`` on the *running* loop and then fell back
    to ``asyncio.run()``; both raise inside async code, so it failed in exactly
    the place agents live. From async code, ``await aenhance(...)`` instead.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_global_engine.execute_enhanced(
            llm_response_text, tool_calls, preview_checks))

    raise RuntimeError(
        "enhance() was called from inside a running event loop, where it cannot "
        "block. Use `await aenhance(...)` instead.")


# -- SDK interception ---------------------------------------------------------
# Each entry: key -> (module path, dotted attribute of the method to wrap).
_PATCH_TARGETS: Dict[str, tuple[str, str]] = {
    "openai": ("openai.resources.chat.completions", "Completions.create"),
    "anthropic": ("anthropic.resources.messages", "Messages.create"),
    "gemini": ("google.generativeai", "GenerativeModel.generate_content"),
    "ollama": ("ollama", "chat"),
}

# key -> (owner_object, attribute_name, original_callable). Enables unpatch_all().
_PATCHED: Dict[str, tuple[Any, str, Any]] = {}


def _resolve(module_path: str, dotted_attr: str) -> tuple[Any, str]:
    """Import module_path and walk dotted_attr, returning (owner, final_attr)."""
    import importlib

    obj = importlib.import_module(module_path)
    parts = dotted_attr.split(".")
    for part in parts[:-1]:
        obj = getattr(obj, part)
    return obj, parts[-1]


def patch_all(*, record: Optional[Callable[[str, Any], None]] = None) -> Dict[str, str]:
    """Wrap the completion method of every LLM SDK that is actually installed.

    Returns a truthful per-SDK report. Values are one of:

    ``"patched"``           the method really was wrapped
    ``"not_installed"``     the SDK is not importable in this environment
    ``"already_patched"``   a previous call already wrapped it
    ``"failed: <reason>"``  the SDK is present but its shape did not match

    This does *not* make a model smarter -- it routes calls through agent-saga so
    they are recorded and inspectable. Monkey-patching third-party SDKs is
    global and version-fragile by nature, so it is opt-in, reversible via
    :func:`unpatch_all`, and never reports success it did not achieve. A safety
    layer that overstates its own coverage is worse than none, because the user
    stops looking.
    """
    report: Dict[str, str] = {}

    for key, (module_path, dotted_attr) in _PATCH_TARGETS.items():
        if key in _PATCHED:
            report[key] = "already_patched"
            continue
        try:
            owner, attr = _resolve(module_path, dotted_attr)
        except ImportError:
            report[key] = "not_installed"
            continue
        except AttributeError as exc:
            report[key] = f"failed: {exc}"
            continue

        original = getattr(owner, attr, None)
        if not callable(original):
            report[key] = f"failed: {module_path}.{dotted_attr} is not callable"
            continue

        setattr(owner, attr, _wrap_sdk_call(key, original, record))
        _PATCHED[key] = (owner, attr, original)
        report[key] = "patched"

    patched = [k for k, v in report.items() if v == "patched"]
    if patched:
        logger.info("agent-saga: intercepting LLM calls for %s", ", ".join(sorted(patched)))
    else:
        logger.warning(
            "agent-saga: patch_all() patched nothing -- no supported LLM SDK is "
            "installed. Report: %s", report)
    return report


def _wrap_sdk_call(provider: str, original: Callable[..., Any],
                   record: Optional[Callable[[str, Any], None]]) -> Callable[..., Any]:
    """Wrap one SDK method so its call is observed. The wrapper is deliberately
    transparent: it never alters the response, and an error in our own bookkeeping
    must never break the user's LLM call."""
    import functools

    def _observe(result: Any) -> None:
        if record is None:
            return
        try:
            record(provider, result)
        except Exception:                     # our bookkeeping, never their call
            logger.exception("agent-saga: record hook failed for %s", provider)

    if asyncio.iscoroutinefunction(original):
        @functools.wraps(original)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            result = await original(*args, **kwargs)
            _observe(result)
            return result
        async_wrapper.__agent_saga_patched__ = True
        return async_wrapper

    @functools.wraps(original)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        _observe(result)
        return result
    wrapper.__agent_saga_patched__ = True
    return wrapper


def unpatch_all() -> Dict[str, str]:
    """Restore every SDK method :func:`patch_all` replaced. Global patching that
    cannot be undone is a trap in tests and notebooks."""
    report: Dict[str, str] = {}
    for key, (owner, attr, original) in list(_PATCHED.items()):
        setattr(owner, attr, original)
        del _PATCHED[key]
        report[key] = "restored"
    return report


__all__ = [
    "UniversalAgentEngine",
    "EnhancedAIResponse",
    "enhance",
    "aenhance",
    "patch_all",
    "unpatch_all",
]
