"""`agent-saga adopt` -- wire an existing agent project up, mechanically.

The honest competitive weakness of this package is not capability, it is the
first twenty minutes. CrewAI wins beginners because starting is easy. Here the
starting instruction has effectively been "read 3,700 lines of documentation,
then classify every side effect your agent can cause." The classification is
genuinely necessary. Reading 3,700 lines first is not.

So this does the mechanical half: it indexes a project, works out which agent
frameworks are in use, finds the tools, and writes the wrapping module for
them. What it will not do is the judgement half.

**It never guesses semantics.** Whether an effect is REVERSIBLE, COMPENSABLE,
or IRREVERSIBLE is the decision the whole engine rests on, and a plausible
default here would be the most expensive kind of wrong -- an emailer quietly
marked COMPENSABLE looks protected and is not. So the generated module emits
`semantics=DECIDE` for every tool, and `DECIDE` is a sentinel that raises the
moment it is used. The file does not run until a human has been through it.
That is deliberate friction in the one place friction pays.

Name-based hints are offered (`send`, `charge`, `delete`, `drop` and friends
are called out as probably irreversible or high-risk) and labelled as hints,
because a hint that reads like a verdict is just a guess wearing a hat.

    agent-saga adopt                     # report what it found
    agent-saga adopt --out saga_tools.py # and write the wrapping module
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger("agent_saga.adopt")

__all__ = ["AdoptionPlan", "ToolCandidate", "analyse"]

#: import root -> the framework it indicates
_FRAMEWORKS = {
    "langgraph": "langgraph", "langchain": "langchain", "langchain_core": "langchain",
    "crewai": "crewai", "autogen": "autogen", "llama_index": "llamaindex",
    "openai": "openai", "anthropic": "anthropic", "semantic_kernel": "semantic-kernel",
}

#: decorators that mark a function as an agent tool
_TOOL_DECORATORS = {"tool", "function_tool", "agent_tool", "ai_callable"}

#: verbs whose effects are usually not undoable. Hints, not classifications.
_IRREVERSIBLE_HINTS = ("send", "email", "sms", "notify", "publish", "post",
                       "transfer", "wire", "payout", "purge", "drop")
_HIGH_RISK_HINTS = ("charge", "pay", "refund", "delete", "remove", "terminate",
                    "destroy", "revoke", "cancel")


@dataclass(frozen=True)
class ToolCandidate:
    name: str
    module: str
    kind: str                  # decorated | structured_tool | basetool_subclass
    lineno: int
    hint: Optional[str] = None

    def describe(self) -> dict:
        return {"name": self.name, "module": self.module, "kind": self.kind,
                "lineno": self.lineno, "hint": self.hint}


@dataclass(frozen=True)
class AdoptionPlan:
    root: str
    frameworks: Tuple[str, ...]
    candidates: Tuple[ToolCandidate, ...]
    modules_scanned: int = 0

    @property
    def needs_review(self) -> Tuple[ToolCandidate, ...]:
        return tuple(c for c in self.candidates if c.hint)

    def describe(self) -> dict:
        return {"root": self.root, "frameworks": list(self.frameworks),
                "modules_scanned": self.modules_scanned,
                "tools": [c.describe() for c in self.candidates]}

    def format_text(self) -> str:
        lines = [f"scanned {self.modules_scanned} module(s) under {self.root}"]
        lines.append(f"  frameworks detected : "
                     f"{', '.join(self.frameworks) or 'none'}")
        lines.append(f"  tools found         : {len(self.candidates)}")
        if not self.candidates:
            lines.append("")
            lines.append("  No tools found. agent-saga wraps the calls that cause")
            lines.append("  side effects; if this project has none yet, there is")
            lines.append("  nothing here to protect and nothing to adopt.")
            return "\n".join(lines)

        lines.append("")
        for candidate in self.candidates:
            lines.append(f"  {candidate.module}.{candidate.name}  "
                         f"({candidate.kind}, line {candidate.lineno})")
            if candidate.hint:
                lines.append(f"      hint: {candidate.hint}")

        lines.append("")
        lines.append("  Next: every tool needs its semantics declared. That is the")
        lines.append("  one thing this command will not decide for you -- an")
        lines.append("  emailer quietly marked COMPENSABLE looks protected and is")
        lines.append("  not. Write the module with --out and fill in each DECIDE.")
        return "\n".join(lines)

    def render_module(self) -> str:
        """The wrapping module, with every semantics left as a sentinel that
        raises until a human replaces it."""
        # A plain string, not an f-string: `.format()` fills `{fleet_name}`
        # below, and doubling every brace for both would make the template
        # unreadable.
        header = '''"""Saga wrappers for this project's tools -- generated by `agent-saga adopt`.

EVERY tool below needs three decisions that only you can make:

  1. **semantics** -- replace each `DECIDE` with one of:
       ActionSemantics.REVERSIBLE    restored exactly; no observer can tell
       ActionSemantics.COMPENSABLE   offset by an inverse; the trace remains
       ActionSemantics.IRREVERSIBLE  no automated undo exists -- gated, not undone
     `DECIDE` raises if it is ever used, so this file will not run until each
     one has been answered. That is on purpose.

  2. **compensate** -- for COMPENSABLE tools, how to undo it. Prefer a
     registered handler so a recovery daemon in another process can run it
     after a crash:

        @compensator("billing.refund")
        def refund(charge_id: str): ...

     then `compensate=delete_by(refund, id_field="id")`, or the general
     `map_result(refund, {{"charge_id": "id"}})`.

  3. **the call** -- each `_call_*` below is a stub. Point it at the real
     function or the framework's tool invocation.

Once filled in, register them with a fleet and audit the result:

    fleet.assert_fully_covered()
"""

from agent_saga import ActionSemantics, Compensation, SagaFleet
from agent_saga.inverses import call_with, delete_by, map_result
from agent_saga.registry import compensator


class _Undecided:
    """Placeholder for a semantics decision nobody has made yet.

    It raises rather than defaulting, because a default here is a silent
    classification of somebody's side effects.
    """

    def __repr__(self) -> str:
        return "DECIDE"

    def __getattr__(self, item):
        raise RuntimeError(
            "a tool's semantics has not been decided. Replace DECIDE with "
            "ActionSemantics.REVERSIBLE, .COMPENSABLE, or .IRREVERSIBLE. The "
            "engine will not guess: it is the one judgement the rest of the "
            "guarantee is built on.")


DECIDE = _Undecided()

fleet = SagaFleet("{fleet_name}")

'''
        body: List[str] = []
        for candidate in self.candidates:
            safe = candidate.name.replace(".", "_")
            comment = f"    # hint: {candidate.hint}\n" if candidate.hint else ""
            body.append(f'''
async def _call_{safe}(**kwargs):
    """Invoke {candidate.module}.{candidate.name}. Replace this stub."""
    raise NotImplementedError(
        "point _call_{safe} at {candidate.module}.{candidate.name}")


{comment}{safe} = fleet.register(
    _call_{safe},
    name="{candidate.module}.{candidate.name}",
    framework="{self.frameworks[0] if self.frameworks else 'unknown'}",
    semantics=DECIDE,                 # <- decide, then delete this comment
    compensate=None,                  # <- required unless IRREVERSIBLE
)
''')
        return header.format(fleet_name=Path(self.root).name or "fleet") + "".join(body)


def analyse(root) -> AdoptionPlan:
    """Index a project and report its frameworks and tools."""
    from .codemod.index import IndexError_, SymbolIndex

    root_path = Path(root).resolve()
    try:
        index = SymbolIndex.build(root_path)
    except IndexError_:
        # A project that does not fully parse is normal in the wild; fall back
        # to per-file best effort rather than refusing to help at all.
        return _analyse_loosely(root_path)

    frameworks: Set[str] = set()
    for imported in index.imports.values():
        for module in imported:
            root_pkg = module.split(".")[0]
            if root_pkg in _FRAMEWORKS:
                frameworks.add(_FRAMEWORKS[root_pkg])

    candidates: List[ToolCandidate] = []
    for name, info in index.modules.items():
        candidates.extend(_find_tools(info.tree, name))

    return AdoptionPlan(
        root=str(root_path), frameworks=tuple(sorted(frameworks)),
        candidates=tuple(candidates), modules_scanned=len(index.modules))


def _analyse_loosely(root_path: Path) -> AdoptionPlan:
    frameworks: Set[str] = set()
    candidates: List[ToolCandidate] = []
    scanned = 0
    for path in sorted(root_path.rglob("*.py")):
        if {".git", "__pycache__", ".venv", "venv", "node_modules"} & set(
                path.relative_to(root_path).parts):
            continue
        try:
            tree = ast.parse(path.read_text("utf-8", errors="replace"))
        except (SyntaxError, ValueError):
            continue
        scanned += 1
        module = path.relative_to(root_path).with_suffix("").as_posix().replace("/", ".")
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    key = alias.name.split(".")[0]
                    if key in _FRAMEWORKS:
                        frameworks.add(_FRAMEWORKS[key])
            elif isinstance(node, ast.ImportFrom) and node.module:
                key = node.module.split(".")[0]
                if key in _FRAMEWORKS:
                    frameworks.add(_FRAMEWORKS[key])
        candidates.extend(_find_tools(tree, module))

    return AdoptionPlan(root=str(root_path), frameworks=tuple(sorted(frameworks)),
                        candidates=tuple(candidates), modules_scanned=scanned)


def _find_tools(tree: ast.Module, module: str) -> List[ToolCandidate]:
    found: List[ToolCandidate] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(_decorator_name(d) in _TOOL_DECORATORS for d in node.decorator_list):
                found.append(ToolCandidate(
                    name=node.name, module=module, kind="decorated",
                    lineno=node.lineno, hint=_hint(node.name)))
        elif isinstance(node, ast.ClassDef):
            if any(_base_name(b) in ("BaseTool", "Tool") for b in node.bases):
                found.append(ToolCandidate(
                    name=node.name, module=module, kind="basetool_subclass",
                    lineno=node.lineno, hint=_hint(node.name)))
        elif isinstance(node, ast.Call):
            if (_attr_chain(node.func).endswith("StructuredTool.from_function")
                    and node.args and isinstance(node.args[0], ast.Name)):
                target = node.args[0].id
                found.append(ToolCandidate(
                    name=target, module=module, kind="structured_tool",
                    lineno=node.lineno, hint=_hint(target)))

    # de-duplicate on (module, name), keeping the first sighting
    seen: Set[Tuple[str, str]] = set()
    unique: List[ToolCandidate] = []
    for candidate in found:
        key = (candidate.module, candidate.name)
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _hint(name: str) -> Optional[str]:
    lowered = name.lower()
    for verb in _IRREVERSIBLE_HINTS:
        if verb in lowered:
            return (f"the name contains {verb!r}, which usually means no "
                    f"automated undo exists -- consider IRREVERSIBLE, which is "
                    f"gated before it runs rather than undone after. A hint, "
                    f"not a classification.")
    for verb in _HIGH_RISK_HINTS:
        if verb in lowered:
            return (f"the name contains {verb!r}: if this touches money or "
                    f"deletes data, its inverse needs a registered handler so a "
                    f"crash can still undo it. A hint, not a classification.")
    return None


def _decorator_name(node: ast.AST) -> str:
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _base_name(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _attr_chain(node: ast.AST) -> str:
    parts: List[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))
