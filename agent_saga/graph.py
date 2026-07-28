"""Graph export: the rollback fork, drawn.

A forward path reads fine as a list of log lines. A *rollback* does not: it
runs LIFO, it branches per step, and its three outcomes carry completely
different consequences -- compensated (clean), compensation failed (dirty, a
human is needed), orphaned (the effect was irreversible and no undo existed).
In a terminal those three are three similar-looking lines scrolling past. On
a graph they are three different shapes in three different colours, and the
one that needs a person is impossible to miss.

That is the whole purpose of this module, and it constrains the design:

  1. **Honest rendering is the contract.** A partial rollback must never draw
     like a clean one. `COMPENSATION_FAILED` and `STEP_ORPHANED` get their own
     styles and their own legend entries -- the `RollbackReport.clean` vs
     `partial` doctrine, expressed in pixels. A diagram that flattered a dirty
     rollback would be worse than no diagram, for exactly the reason a WAL
     that swallowed a corrupt record would be worse than no WAL.

  2. **Reconstruction is total.** A WAL is evidence, and evidence arrives
     damaged: truncated writes, records from an older version, fields of the
     wrong type, hostile input. Every function here renders what it can and
     labels the rest `unknown`. None of them raise on a malformed record. (The
     same commitment the WAL reader makes -- see `ui/reader.py`.)

  3. **User data never becomes syntax.** Tool names, node ids, and saga names
     are attacker-influenced in the general case: an agent names its own
     tools. Identifiers in the output are synthetic (`n0`, `n1`, ...) and user
     text appears only inside escaped labels. A tool called
     `"] --> evil["pwned` produces a diagram with a funny label, not a diagram
     someone else authored.

  4. **Output is deterministic.** Same records, same bytes -- so a diagram can
     be committed, diffed in review, and regenerated in CI.

Both renderers are pure text with no dependencies. Mermaid pastes into
GitHub/GitLab markdown and renders natively; DOT feeds Graphviz for PNG/SVG.

    from agent_saga.graph import wal_to_mermaid
    print(wal_to_mermaid(await wal.read_all()))

or from the shell, over a WAL file:

    agent-saga graph --wal ./agent-saga.wal --format mermaid
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "dag_to_dot",
    "dag_to_mermaid",
    "wal_to_dot",
    "wal_to_mermaid",
]

_MAX_LABEL = 80
"""Labels are cropped, not wrapped. A 4 KB tool argument in a node box makes
the graph unreadable, which defeats the only reason this module exists."""

# Forward-path step states, in the order they may overwrite one another.
_FORWARD_EVENTS = {
    "STEP_INTENT": "intent",
    "STEP_COMMITTED": "committed",
    "STEP_UNKNOWN": "unknown",
    "COMPLETED_VIA_FALLBACK": "fallback",
}

# Rollback outcomes. These are the three the graph exists to distinguish.
_ROLLBACK_EVENTS = {
    "COMPENSATED": "compensated",
    "COMPENSATION_FAILED": "compensation_failed",
    "STEP_ORPHANED": "orphaned",
}

_STATE_TEXT = {
    "intent": "intent logged, outcome unknown",
    "committed": "committed",
    "unknown": "UNKNOWN - effect may have landed",
    "fallback": "completed via fallback",
    "compensated": "compensated",
    "compensation_failed": "COMPENSATION FAILED - needs a human",
    "orphaned": "ORPHANED - no undo exists",
}


@dataclass
class _Step:
    """One reconstructed step. Fields stay Optional because a damaged WAL is a
    normal input, not an exceptional one."""

    key: str
    tool: str = "unknown"
    semantics: str = ""
    state: str = "intent"
    rollback: Optional[str] = None
    detail: str = ""
    order: int = 0


def _text(value: Any, fallback: str = "unknown") -> str:
    """Coerce any WAL field to display text. Never raises: a WAL that survived
    a crash mid-write can hold a dict where a string belongs."""
    if value is None:
        return fallback
    if isinstance(value, str):
        return value or fallback
    return repr(value)


def _crop(text: str) -> str:
    text = "".join(ch if ch.isprintable() else " " for ch in text).strip()
    if len(text) <= _MAX_LABEL:
        return text
    return text[: _MAX_LABEL - 3] + "..."


def _mermaid_label(*parts: str) -> str:
    """Build a Mermaid label from parts: each part is escaped as untrusted
    data, then joined with the `<br/>` line break we control.

    The separator must be applied AFTER escaping, never before. Escaping an
    already-assembled label would eat our own `<br/>` into `&lt;br/&gt;` and
    print the tag at the user instead of breaking the line -- and relaxing the
    `<`/`>` escape to avoid that would hand every tool name the ability to
    inject markup. Parts in, syntax out.
    """
    return "<br/>".join(_escape_mermaid(part) for part in parts if part)


def _escape_mermaid(text: str) -> str:
    """Escape one untrusted fragment. Mermaid has no backslash escape, so the
    quote becomes an HTML entity -- the documented way -- and the brackets that
    could close a node shape are entity-escaped too."""
    out = _crop(text)
    out = out.replace("&", "&amp;").replace('"', "#quot;")
    out = out.replace("<", "&lt;").replace(">", "&gt;")
    return out.replace("[", "&#91;").replace("]", "&#93;")


def _dot_label(*parts: str) -> str:
    """Build a DOT label from parts, joined with DOT's own newline escape.

    Same rule as `_mermaid_label`: the `\\n` separator is emitted after each
    part is escaped, because `_escape_dot` doubles backslashes and would
    otherwise turn our line break into a literal backslash-n on the diagram.
    """
    return "\\n".join(_escape_dot(part) for part in parts if part)


def _escape_dot(text: str) -> str:
    """Escape one untrusted fragment for a DOT quoted string: backslash first,
    then the quote."""
    return _crop(text).replace("\\", "\\\\").replace('"', '\\"')


def _records(records: Any) -> List[Mapping[str, Any]]:
    """Accept any iterable; keep only mapping-shaped entries. A list with a
    stray None in it is a damaged log, not a crash."""
    if records is None:
        return []
    try:
        candidates = list(records)
    except TypeError:
        return []
    return [r for r in candidates if isinstance(r, Mapping)]


def _reconstruct(records: Any) -> Tuple[List[_Step], Dict[str, Any]]:
    """Fold WAL records into ordered steps plus saga-level facts.

    Correlation is by `step_id` when present and by tool name otherwise, so a
    log written before step ids existed still draws -- degraded, never blank.
    """
    steps: Dict[str, _Step] = {}
    meta: Dict[str, Any] = {"saga_id": None, "name": None, "terminal": None,
                            "cause": None, "rolled_back": False}

    for record in _records(records):
        event = record.get("event")
        if not isinstance(event, str):
            continue

        if event == "SAGA_START":
            meta["saga_id"] = _text(record.get("saga_id"), "")
            meta["name"] = _text(record.get("name"), "")
            continue
        if event in ("SAGA_COMPLETE", "SAGA_ABORTED"):
            meta["terminal"] = event
            continue
        if event == "SAGA_ABORT_CAUSE":
            meta["cause"] = _text(record.get("cause") or record.get("error"), "")
            continue
        if event == "ROLLBACK_START":
            meta["rolled_back"] = True
            continue

        forward = _FORWARD_EVENTS.get(event)
        rollback = _ROLLBACK_EVENTS.get(event)
        if forward is None and rollback is None:
            continue

        tool = _text(record.get("tool"))
        raw_key = record.get("step_id")
        key = _text(raw_key, "") or f"tool:{tool}"
        step = steps.get(key)
        if step is None:
            step = _Step(key=key, tool=tool, order=len(steps))
            steps[key] = step
        if step.tool == "unknown" and tool != "unknown":
            step.tool = tool

        semantics = record.get("semantics")
        if isinstance(semantics, str) and semantics:
            step.semantics = semantics

        if forward is not None:
            step.state = forward
            if forward == "unknown":
                step.detail = _text(record.get("error"), "")
        else:
            step.rollback = rollback
            if rollback == "compensation_failed":
                step.detail = _text(record.get("error"), "")

    return sorted(steps.values(), key=lambda s: s.order), meta


def _saga_title(meta: Mapping[str, Any]) -> str:
    name = meta.get("name") or ""
    saga_id = meta.get("saga_id") or ""
    if name and saga_id:
        return f"saga {name} ({saga_id})"
    return f"saga {name or saga_id or 'unknown'}"


def _terminal_text(meta: Mapping[str, Any]) -> Tuple[str, str]:
    terminal = meta.get("terminal")
    if terminal == "SAGA_COMPLETE":
        return "SAGA_COMPLETE", "ok"
    if terminal == "SAGA_ABORTED":
        cause = meta.get("cause") or ""
        return (f"SAGA_ABORTED - {cause}" if cause else "SAGA_ABORTED"), "bad"
    # No terminal record: the process died, or the log is truncated. Say so.
    return "no terminal record - process died or log truncated", "warn"


# -- WAL execution trace ---------------------------------------------------------

def wal_to_mermaid(records: Any, *, title: Optional[str] = None) -> str:
    """Render an executed saga as a Mermaid flowchart: the forward path, and
    the rollback fork branching off it per step.

    Renders whatever the records support. An empty or unreadable log produces
    a valid diagram saying exactly that, because a blank screen and a broken
    export are indistinguishable to the person looking at it.
    """
    steps, meta = _reconstruct(records)
    lines = ["flowchart TD"]
    heading = title or _saga_title(meta)
    lines.append(f'  start(["{_mermaid_label(heading)}"])')

    if not steps:
        lines.append('  empty["no step records found in this log"]')
        lines.append("  start --> empty")
        lines.append("  class empty warn")
        lines.extend(_MERMAID_CLASSES)
        return "\n".join(lines)

    forward_ids: List[str] = []
    styled: Dict[str, List[str]] = {}

    for index, step in enumerate(steps):
        node = f"n{index}"
        forward_ids.append(node)
        head = f"{index + 1}. {step.tool}"
        if step.semantics:
            head += f" [{step.semantics}]"
        label = _mermaid_label(head, _STATE_TEXT.get(step.state, step.state))
        lines.append(f'  {node}["{label}"]')
        styled.setdefault(_forward_class(step.state), []).append(node)

    lines.append("  start --> " + " --> ".join(forward_ids))

    # The fork. Drawn as dotted branches off the forward chain so the eye
    # reads the happy path straight down and the undo path sideways.
    for index, step in enumerate(steps):
        if step.rollback is None:
            continue
        node = f"n{index}"
        comp = f"c{index}"
        label = _mermaid_label(_STATE_TEXT.get(step.rollback, step.rollback), step.detail)
        lines.append(f'  {comp}["{label}"]')
        lines.append(f"  {node} -.-> {comp}")
        styled.setdefault(_rollback_class(step.rollback), []).append(comp)

    terminal_text, terminal_kind = _terminal_text(meta)
    lines.append(f'  done(["{_mermaid_label(terminal_text)}"])')
    lines.append(f"  {forward_ids[-1]} --> done")
    styled.setdefault({"ok": "ok", "bad": "bad", "warn": "warn"}[terminal_kind], []).append("done")

    for css_class, nodes in styled.items():
        lines.append(f"  class {','.join(nodes)} {css_class}")
    lines.extend(_MERMAID_CLASSES)
    return "\n".join(lines)


def wal_to_dot(records: Any, *, title: Optional[str] = None) -> str:
    """The same execution trace as Graphviz DOT, for PNG/SVG rendering."""
    steps, meta = _reconstruct(records)
    heading = title or _saga_title(meta)
    lines = [
        "digraph saga {",
        "  rankdir=TB;",
        '  graph [fontname="Helvetica"];',
        '  node [shape=box, style="rounded,filled", fontname="Helvetica", '
        'fillcolor="#f5f5f5", color="#999999"];',
        '  edge [fontname="Helvetica", color="#666666"];',
        f'  start [label="{_dot_label(heading)}", shape=oval, fillcolor="#e8eefc"];',
    ]

    if not steps:
        lines.append('  empty [label="no step records found in this log", '
                     'fillcolor="#fff4d6", color="#d9a441"];')
        lines.append("  start -> empty;")
        lines.append("}")
        return "\n".join(lines)

    previous = "start"
    for index, step in enumerate(steps):
        node = f"n{index}"
        head = f"{index + 1}. {step.tool}"
        if step.semantics:
            head += f" [{step.semantics}]"
        label = _dot_label(head, _STATE_TEXT.get(step.state, step.state))
        fill, stroke = _DOT_COLOURS[_forward_class(step.state)]
        lines.append(f'  {node} [label="{label}", fillcolor="{fill}", '
                     f'color="{stroke}"];')
        lines.append(f"  {previous} -> {node};")
        previous = node

    for index, step in enumerate(steps):
        if step.rollback is None:
            continue
        comp = f"c{index}"
        label = _dot_label(_STATE_TEXT.get(step.rollback, step.rollback), step.detail)
        fill, stroke = _DOT_COLOURS[_rollback_class(step.rollback)]
        lines.append(f'  {comp} [label="{label}", fillcolor="{fill}", '
                     f'color="{stroke}"];')
        lines.append(f'  n{index} -> {comp} [style=dashed, label="undo"];')

    terminal_text, terminal_kind = _terminal_text(meta)
    fill, stroke = _DOT_COLOURS[terminal_kind]
    lines.append(f'  done [label="{_dot_label(terminal_text)}", shape=oval, '
                 f'fillcolor="{fill}", color="{stroke}"];')
    lines.append(f"  {previous} -> done;")
    lines.append("}")
    return "\n".join(lines)


# -- DAG plan ---------------------------------------------------------------------

def dag_to_mermaid(dag: Any, *, title: Optional[str] = None) -> str:
    """Render a `DAGSaga` plan: nodes, dependency edges, and each node's live
    status. Works before execution (everything PENDING), during, and after."""
    nodes = _dag_nodes(dag)
    lines = ["flowchart TD"]
    heading = title or f"DAG {getattr(dag, 'name', 'dag_saga')}"
    lines.append(f"  %% {_mermaid_label(heading)}")

    if not nodes:
        lines.append('  empty["no nodes registered"]')
        lines.append("  class empty warn")
        lines.extend(_MERMAID_CLASSES)
        return "\n".join(lines)

    ids = {node_id: f"n{index}" for index, (node_id, _) in enumerate(nodes)}
    styled: Dict[str, List[str]] = {}

    for node_id, node in nodes:
        status = _text(getattr(node, "status", "PENDING"), "PENDING")
        label = _mermaid_label(node_id,
                               _text(getattr(node, "description", ""), ""),
                               status,
                               _text(getattr(node, "error", None), ""))
        lines.append(f'  {ids[node_id]}["{label}"]')
        styled.setdefault(_dag_class(status), []).append(ids[node_id])

    for node_id, node in nodes:
        for dependency in _dependencies(node):
            if dependency in ids:
                lines.append(f"  {ids[dependency]} --> {ids[node_id]}")
            else:
                # A dependency on an unregistered node is a real defect the
                # plan would raise on at sort time. Draw it rather than drop
                # it: an invisible broken edge is how a bad plan looks fine.
                missing = f"missing_{abs(hash(dependency)) % 10000}"
                lines.append(f'  {missing}["{_mermaid_label(dependency, "MISSING")}"]')
                lines.append(f"  {missing} --> {ids[node_id]}")
                styled.setdefault("bad", []).append(missing)

    for css_class, node_ids in styled.items():
        lines.append(f"  class {','.join(node_ids)} {css_class}")
    lines.extend(_MERMAID_CLASSES)
    return "\n".join(lines)


def dag_to_dot(dag: Any, *, title: Optional[str] = None) -> str:
    """The same plan as Graphviz DOT."""
    nodes = _dag_nodes(dag)
    heading = title or f"DAG {getattr(dag, 'name', 'dag_saga')}"
    lines = [
        "digraph dag {",
        "  rankdir=TB;",
        f'  label="{_dot_label(heading)}";',
        '  graph [fontname="Helvetica"];',
        '  node [shape=box, style="rounded,filled", fontname="Helvetica"];',
    ]
    if not nodes:
        lines.append('  empty [label="no nodes registered", fillcolor="#fff4d6"];')
        lines.append("}")
        return "\n".join(lines)

    ids = {node_id: f"n{index}" for index, (node_id, _) in enumerate(nodes)}
    for node_id, node in nodes:
        status = _text(getattr(node, "status", "PENDING"), "PENDING")
        label = _dot_label(node_id,
                           _text(getattr(node, "description", ""), ""),
                           status)
        fill, stroke = _DOT_COLOURS[_dag_class(status)]
        lines.append(f'  {ids[node_id]} [label="{label}", '
                     f'fillcolor="{fill}", color="{stroke}"];')

    for node_id, node in nodes:
        for dependency in _dependencies(node):
            if dependency in ids:
                lines.append(f"  {ids[dependency]} -> {ids[node_id]};")
            else:
                missing = f"missing{abs(hash(dependency)) % 10000}"
                fill, stroke = _DOT_COLOURS["bad"]
                lines.append(f'  {missing} [label="{_dot_label(dependency, "MISSING")}", '
                             f'fillcolor="{fill}", color="{stroke}"];')
                lines.append(f"  {missing} -> {ids[node_id]};")
    lines.append("}")
    return "\n".join(lines)


def _dag_nodes(dag: Any) -> List[Tuple[str, Any]]:
    nodes = getattr(dag, "nodes", None)
    if not isinstance(nodes, Mapping):
        return []
    return [(_text(k, "unknown"), v) for k, v in nodes.items()]


def _dependencies(node: Any) -> List[str]:
    dependencies = getattr(node, "dependencies", None)
    if not isinstance(dependencies, (list, tuple)):
        return []
    return [_text(d, "unknown") for d in dependencies]


# -- styling ------------------------------------------------------------------------

def _forward_class(state: str) -> str:
    if state == "committed":
        return "ok"
    if state == "fallback":
        return "info"
    if state == "unknown":
        return "warn"
    return "pending"


def _rollback_class(outcome: str) -> str:
    # The one mapping that must never be softened: a failed compensation and an
    # orphaned effect are `bad`, and they look nothing like `compensated`.
    if outcome == "compensated":
        return "undo"
    return "bad"


def _dag_class(status: str) -> str:
    return {"COMPLETED": "ok", "RUNNING": "info", "FAILED": "bad"}.get(status, "pending")


_MERMAID_CLASSES = [
    "  classDef ok fill:#0f3d2e,stroke:#22c55e,color:#dcfce7;",
    "  classDef undo fill:#0e2f4a,stroke:#38bdf8,color:#e0f2fe;",
    "  classDef info fill:#2a2350,stroke:#a78bfa,color:#ede9fe;",
    "  classDef warn fill:#4a3a10,stroke:#f59e0b,color:#fef3c7;",
    "  classDef bad fill:#4c1220,stroke:#ef4444,color:#fee2e2;",
    "  classDef pending fill:#26262b,stroke:#71717a,color:#e4e4e7;",
]

_DOT_COLOURS: Dict[str, Tuple[str, str]] = {
    "ok": ("#dcfce7", "#22c55e"),
    "undo": ("#e0f2fe", "#38bdf8"),
    "info": ("#ede9fe", "#a78bfa"),
    "warn": ("#fef3c7", "#f59e0b"),
    "bad": ("#fee2e2", "#ef4444"),
    "pending": ("#f4f4f5", "#71717a"),
}
