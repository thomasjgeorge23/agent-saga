"""Tool policy for the MCP proxy: what each tool is, and what undoes it.

In the library, the developer writing the tool declares its semantics and its
inverse. Through a proxy there is no such developer -- the agent calls whatever
its MCP servers expose, and nobody has said which of those calls move money.

So the declaration moves from code to a policy file. That is the point rather
than a concession: the person who should decide whether `create_charge` needs a
human is not the person who wrote the agent, and a file is something a security
team can review, diff, and sign off. The agent itself changes not at all.

The unavoidable question is what an *undeclared* tool does. Both obvious answers
are wrong: refusing everything makes the proxy unusable until every tool in
every server is classified, and allowing everything makes it a no-op that logs.
So there are two modes, and the ramp between them is the product:

  * OBSERVE   -- forward everything, classify nothing, record exactly which
                 tools were called and with what shape. Produces the policy
                 skeleton you could not have written by hand, from real traffic.
  * ENFORCE   -- undeclared tools are refused. Nothing reaches a real system
                 without someone having said what it is.

Run observe, generate, review, enforce. A safety control that cannot be adopted
incrementally does not get adopted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from ..semantics import ActionSemantics


class PolicyError(Exception):
    """The policy file is wrong in a way that would silently weaken the proxy."""


@dataclass(frozen=True)
class CompensationSpec:
    """How to undo one tool, expressed as another tool call.

    `args` are extracted from the forward call's *result*, because that is the
    only place a charge id can come from. `from_arguments` are copied from the
    forward call's own arguments, for inverses keyed on what was requested
    rather than what came back.
    """

    tool: str
    args: dict = field(default_factory=dict)
    from_arguments: dict = field(default_factory=dict)
    static: dict = field(default_factory=dict)
    server: Optional[str] = None

    def build(self, result: Any, arguments: dict) -> dict:
        out = dict(self.static)
        for name, path in self.args.items():
            out[name] = extract(result, path)
        for name, path in self.from_arguments.items():
            out[name] = extract(arguments, path)
        return out


@dataclass(frozen=True)
class ToolPolicy:
    name: str
    semantics: ActionSemantics
    compensate: Optional[CompensationSpec] = None
    policy_args: dict = field(default_factory=dict)
    """Arguments the gate evaluates, extracted from the call's arguments.

    Declared separately from the raw arguments for the same reason
    SagaContext.execute takes `policy_args`: a limit on `amount` must read the
    amount, and an MCP tool is free to nest it anywhere in its schema.
    """

    description: str = ""

    def gate_args(self, arguments: dict) -> dict:
        if not self.policy_args:
            return dict(arguments)
        out = dict(arguments)
        for name, path in self.policy_args.items():
            value = extract(arguments, path)
            if value is not None:
                out[name] = value
        return out


@dataclass
class ProxyPolicy:
    mode: str = "enforce"
    tools: dict = field(default_factory=dict)
    unknown_semantics: Optional[ActionSemantics] = None
    """In ENFORCE, what an undeclared tool is treated as. None means refuse.

    Setting this to REVERSIBLE is how a deployment says "my unclassified tools
    are all reads" -- a claim it should have to make explicitly, in a file
    someone signed, rather than inherit as a default.
    """

    @property
    def observing(self) -> bool:
        return self.mode == "observe"

    def get(self, tool: str) -> Optional[ToolPolicy]:
        return self.tools.get(tool)


def extract(source: Any, path: str) -> Any:
    """Resolve a `$.a.b[0]` path against a result or an argument dict.

    Deliberately tiny: a policy file is a security artifact, and a full
    expression language in one would be a place to hide behaviour. Anything a
    path cannot express belongs in a real connector, not in config.

    A path that does not resolve returns None rather than raising -- the caller
    decides whether a missing value is fatal, and for a compensation it is.
    """
    if not isinstance(path, str) or not path.startswith("$"):
        return path                      # a literal, not a path
    current = source
    for part in _tokens(path):
        if current is None:
            return None
        if isinstance(part, int):
            if not isinstance(current, (list, tuple)) or part >= len(current):
                return None
            current = current[part]
        else:
            if not isinstance(current, dict):
                return None
            current = current.get(part)
    return current


def _tokens(path: str) -> list:
    out: list = []
    for chunk in path[1:].split("."):
        if not chunk:
            continue
        name, _, rest = chunk.partition("[")
        if name:
            out.append(name)
        while rest:
            index, _, rest = rest.partition("]")
            if index:
                try:
                    out.append(int(index))
                except ValueError:
                    out.append(index.strip("'\""))
            rest = rest.lstrip("[")
    return out


def _semantics(value: str, tool: str) -> ActionSemantics:
    try:
        return ActionSemantics[str(value).upper()]
    except KeyError:
        raise PolicyError(
            f"tool {tool!r} declares semantics {value!r}; expected one of "
            f"{', '.join(s.name for s in ActionSemantics)}") from None


def load_policy(data: dict) -> ProxyPolicy:
    """Parse and validate. Every check here is one a live proxy cannot make."""
    if not isinstance(data, dict):
        raise PolicyError("policy must be a JSON object")

    mode = str(data.get("mode", "enforce")).lower()
    if mode not in ("enforce", "observe"):
        raise PolicyError(f"mode must be 'enforce' or 'observe', got {mode!r}")

    unknown = data.get("unknown_semantics")
    unknown_sem = _semantics(unknown, "<unknown>") if unknown else None

    tools: dict = {}
    for name, spec in (data.get("tools") or {}).items():
        if isinstance(spec, str):
            spec = {"semantics": spec}
        if not isinstance(spec, dict):
            raise PolicyError(f"tool {name!r} must map to an object or a semantics string")

        semantics = _semantics(spec.get("semantics", ""), name)

        comp_spec = spec.get("compensate")
        compensate = None
        if comp_spec:
            if not isinstance(comp_spec, dict) or not comp_spec.get("tool"):
                raise PolicyError(
                    f"tool {name!r}: `compensate` needs a `tool` naming the call "
                    f"that undoes it")
            compensate = CompensationSpec(
                tool=comp_spec["tool"],
                args=comp_spec.get("args") or {},
                from_arguments=comp_spec.get("from_arguments") or {},
                static=comp_spec.get("static") or {},
                server=comp_spec.get("server"))

        # The check that matters: a tool declared undoable with nothing to undo
        # it with would roll back "cleanly" while the charge stands.
        if semantics is ActionSemantics.COMPENSABLE and compensate is None:
            raise PolicyError(
                f"tool {name!r} is COMPENSABLE but declares no `compensate`. "
                f"That would report a clean rollback while the effect stands. "
                f"Declare the inverse, or mark it IRREVERSIBLE so the gate "
                f"stops it before it runs.")
        if semantics is ActionSemantics.IRREVERSIBLE and compensate is not None:
            raise PolicyError(
                f"tool {name!r} is IRREVERSIBLE but declares a `compensate`. "
                f"One of the two is wrong, and guessing which would be a guess "
                f"about whether a real effect can be undone.")

        tools[name] = ToolPolicy(
            name=name, semantics=semantics, compensate=compensate,
            policy_args=spec.get("policy_args") or {},
            description=spec.get("description", ""))

    return ProxyPolicy(mode=mode, tools=tools, unknown_semantics=unknown_sem)


def load_policy_file(path: str) -> ProxyPolicy:
    with open(path, encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise PolicyError(f"{path} is not valid JSON: {exc}") from exc
    return load_policy(data)


# Name heuristics for a *suggestion* only. The active `semantics` stays
# IRREVERSIBLE regardless; these drive advisory `_suggested_*` fields a reviewer
# acts on. Ordered by precedence: destructive/irreversible wins over write, write
# over read, so "delete_and_read" is not mistaken for a read.
_READ_VERBS = ("get", "list", "read", "search", "fetch", "query", "describe",
               "show", "lookup", "count", "exists", "find", "view")
_WRITE_VERBS = ("create", "add", "insert", "update", "set", "put", "upload",
                "register", "book", "reserve", "charge", "provision", "enable")
_DESTRUCTIVE_VERBS = ("delete", "remove", "destroy", "drop", "purge", "send",
                      "email", "publish", "execute", "run", "pay", "transfer",
                      "refund", "cancel", "terminate", "deploy", "disable")


def _suggest_semantics(name: str) -> str:
    lowered = name.lower()
    if any(v in lowered for v in _DESTRUCTIVE_VERBS):
        return "IRREVERSIBLE"
    if any(v in lowered for v in _WRITE_VERBS):
        return "COMPENSABLE"
    if any(v in lowered for v in _READ_VERBS):
        return "REVERSIBLE"
    return "IRREVERSIBLE"


def _rate_limit_recommendation(seen: dict) -> Optional[dict]:
    """A conservative rate-limit suggestion from observed call frequency: project
    the observed rate over a 60s window and double it for headroom, so a normal
    burst is not throttled but a runaway loop is capped."""
    import math

    calls = seen.get("calls", 0)
    first, last = seen.get("first_seen"), seen.get("last_seen")
    if not calls:
        return None
    window = 60.0
    if first and last and last > first:
        per_second = calls / (last - first)
        recommended = max(1, math.ceil(per_second * window * 2))
        basis = f"observed {calls} call(s) over {last - first:.1f}s"
    else:
        # No usable timing (single call or instantaneous): cap at 2x the count.
        recommended = max(1, calls * 2)
        basis = f"observed {calls} call(s), no timing window"
    return {"max_calls": recommended, "window_seconds": window, "basis": basis}


def skeleton_from_observations(observations: dict) -> dict:
    """Turn what OBSERVE saw into a near-complete policy a human can finish fast.

    The active ``semantics`` is still emitted as IRREVERSIBLE, deliberately. A
    generator that *set* COMPENSABLE and invented an inverse would assert that a
    real financial operation can be undone on the evidence of a tool name --
    the one guess this project exists to refuse. The reviewer downgrades what is
    safe; the file never upgrades itself.

    What the generator now adds are *advisory* fields (``_``-prefixed, ignored by
    the loader) so review is a quick confirm rather than a from-scratch write:

      * ``_suggested_semantics`` -- a name-based guess to accept or correct.
      * ``_compensate_stub`` -- for write-like tools, the shape of the inverse to
        fill in (never active until the reviewer moves it to ``compensate`` and
        sets COMPENSABLE).
      * ``_rate_limit`` -- a cap projected from the observed call frequency.
    """
    tools = {}
    for name, seen in sorted(observations.items()):
        suggestion = _suggest_semantics(name)
        entry = {
            "semantics": "IRREVERSIBLE",
            "description": (
                f"TODO: classify (suggested: {suggestion}). Observed "
                f"{seen.get('calls', 0)} call(s); argument keys seen: "
                f"{sorted(seen.get('arg_keys', []))}."),
            "_suggested_semantics": suggestion,
        }
        rate = _rate_limit_recommendation(seen)
        if rate:
            entry["_rate_limit"] = rate
        if suggestion == "COMPENSABLE":
            entry["_compensate_stub"] = {
                "tool": f"{name.split('.')[-1]}_undo",
                "from_arguments": {k: k for k in sorted(seen.get("arg_keys", []))},
                "note": "TODO: implement the inverse call, then set semantics: "
                        "COMPENSABLE and move this to `compensate`.",
            }
        tools[name] = entry
    return {"mode": "enforce", "tools": tools}


def render_policy_yaml(skeleton: dict) -> str:
    """Render a policy skeleton as YAML text for a reviewer. Uses PyYAML when
    available; otherwise emits a minimal, correct YAML by hand (the structure is
    shallow) so this stays dependency-free."""
    try:
        import yaml  # type: ignore
        return yaml.safe_dump(skeleton, sort_keys=False, default_flow_style=False)
    except Exception:
        return _yaml_dump(skeleton)


def _yaml_dump(obj: Any, indent: int = 0) -> str:
    pad = "  " * indent
    lines: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}{k}:")
                lines.append(_yaml_dump(v, indent + 1))
            else:
                lines.append(f"{pad}{k}: {_yaml_scalar(v)}")
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                lines.append(f"{pad}-")
                lines.append(_yaml_dump(item, indent + 1))
            else:
                lines.append(f"{pad}- {_yaml_scalar(item)}")
    else:
        return f"{pad}{_yaml_scalar(obj)}"
    return "\n".join(l for l in lines if l)


def _yaml_scalar(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if s == "" or any(c in s for c in ":#{}[],&*!|>'\"%@`") or s.strip() != s:
        return json.dumps(s)   # quote anything YAML-ambiguous
    return s


__all__ = [
    "ProxyPolicy", "ToolPolicy", "CompensationSpec", "PolicyError",
    "load_policy", "load_policy_file", "extract", "skeleton_from_observations",
    "render_policy_yaml",
]
