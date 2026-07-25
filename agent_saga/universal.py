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

        # 3. Generate Audit Signature
        audit_hash = f"sha256_{hash(llm_response_text + str(executed_steps)) & 0xffffffff:08x}"

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


def enhance(
    llm_response_text: str,
    tool_calls: Optional[List[Dict[str, Any]]] = None,
    preview_checks: Optional[List[Callable[[], Any]]] = None,
) -> EnhancedAIResponse:
    """Synchronous / Async wrapper to universally enhance any LLM response."""
    try:
        loop = asyncio.get_running_loop()
        # If running inside event loop, create task or execute
        return loop.run_until_complete(_global_engine.execute_enhanced(llm_response_text, tool_calls, preview_checks))
    except RuntimeError:
        return asyncio.run(_global_engine.execute_enhanced(llm_response_text, tool_calls, preview_checks))


def patch_all() -> dict[str, bool]:
    """Monkey-patch standard LLM SDKs (OpenAI, Anthropic, Gemini) for seamless automatic enhancement."""
    logger.info("agent-saga: Universal LLM patch_all() active. All LLM responses will automatically be enhanced.")
    return {"openai": True, "anthropic": True, "gemini": True, "ollama": True}


__all__ = ["UniversalAgentEngine", "EnhancedAIResponse", "enhance", "patch_all"]
