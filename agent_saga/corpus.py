"""The WAL as a labelled corpus: what the agent did, and whether reality took it.

Fine-tuning an agent needs examples of good and bad tool calls. Getting them is
the hard part -- human labelling is slow, and "the model said it confidently" is
not a label. Meanwhile every agent-saga deployment has been writing the labels
down all along, from a source that cannot flatter anyone: **whether the effect
had to be undone.**

A step that committed inside a saga that completed was accepted by the world. A
step that raised is the one that failed. That is ground truth nobody else
collects, because nobody else records the undo.

The engineering that makes this a corpus rather than a pile:

**Blame attribution is the whole problem.** When step 5 fails, steps 1-4 get
rolled back -- and they were *correct*. A naive exporter labels all four
negative and teaches the model to avoid the actions that worked. So a rolled-back
step is labelled `COLLATERAL`, kept out of the trainable set, and the step that
actually raised is the only one labelled `REJECTED`.

**Ambiguity is preserved, not resolved.** A step whose outcome was `UNKNOWN` --
a timed-out call that may or may not have landed -- genuinely does not tell you
whether the action was right. It is labelled `AMBIGUOUS` and excluded. Guessing
here would put noise in the dataset and call it signal.

**Being unrecoverable is not being wrong.** A step reported `ORPHANED` had no
inverse; that is a fact about compensation coverage, not about whether the call
was correct. It travels as a flag, never as a label.

**Gate refusals are visible but not attributable to a step.** The pre-flight
gate raises *before* `STEP_INTENT` is written, so a refused call leaves no step
record at all -- only the abort cause. Those are surfaced at saga level rather
than invented as examples that were never logged.

    corpus = build_corpus(records)
    print(corpus.format_text())          # what signal you actually have
    corpus.to_jsonl(path, redact=["email", "card"])

**Privacy, before you export anything.** A WAL holds real arguments: amounts,
addresses, customer ids. Exporting it for training exports those. `redact=`
drops named fields, `to_jsonl` refuses to write without an explicit
acknowledgement, and the honest alternative for sharing outside your own
infrastructure is a synthetic corpus with the same shape rather than this one.
"""

from __future__ import annotations

import enum
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import (Any, Dict, Iterable, List, Mapping, Optional, Sequence,
                    Set, Tuple)

logger = logging.getLogger("agent_saga.corpus")

__all__ = [
    "Corpus",
    "Example",
    "Label",
    "build_corpus",
]

_REDACTED = "[REDACTED]"


class Label(str, enum.Enum):
    """What the world did with this action."""

    ACCEPTED = "accepted"
    """Committed inside a saga that completed. Reality kept it."""

    REJECTED = "rejected"
    """This step is the one that failed. The negative signal."""

    COLLATERAL = "collateral"
    """Committed, then rolled back only because a LATER step failed. The action
    was correct; it was undone for somebody else's mistake. Excluded from
    training, because teaching a model to avoid these teaches it to avoid the
    calls that worked."""

    AMBIGUOUS = "ambiguous"
    """Outcome UNKNOWN -- the call raised or timed out and may still have
    landed. Genuinely uninformative, so excluded rather than guessed."""


@dataclass(frozen=True)
class Example:
    saga_id: str
    step: int
    tool: str
    arguments: Mapping[str, Any]
    label: Label
    reason: str
    context_hash: Optional[str] = None
    orphaned: bool = False

    @property
    def trainable(self) -> bool:
        """Only unambiguous, correctly-attributed outcomes."""
        return self.label in (Label.ACCEPTED, Label.REJECTED)

    def to_json(self, *, redact: Sequence[str] = ()) -> dict:
        return {
            "saga_id": self.saga_id, "step": self.step, "tool": self.tool,
            "arguments": _redact(self.arguments, redact),
            "label": self.label.value, "reason": self.reason,
            "context_hash": self.context_hash, "orphaned": self.orphaned,
        }


@dataclass(frozen=True)
class Corpus:
    examples: Tuple[Example, ...]
    sagas: int = 0
    gate_refusals: Tuple[str, ...] = ()
    """Sagas aborted by the pre-flight gate. Surfaced, not turned into examples:
    the gate refuses before `STEP_INTENT` is written, so no step was ever
    logged for the call it stopped."""

    @property
    def counts(self) -> Dict[str, int]:
        out = {label.value: 0 for label in Label}
        for example in self.examples:
            out[example.label.value] += 1
        return out

    @property
    def trainable(self) -> Tuple[Example, ...]:
        return tuple(e for e in self.examples if e.trainable)

    @property
    def with_context(self) -> Tuple[Example, ...]:
        """Examples linked to the exact context the model saw. Without it you
        have an action and no prompt, which is far weaker training signal."""
        return tuple(e for e in self.trainable if e.context_hash)

    def preference_pairs(self) -> Tuple[Tuple[Example, Example], ...]:
        """(accepted, rejected) pairs for the same tool -- the shape DPO wants.

        Paired within a tool, because "a good charge versus a bad email" teaches
        nothing about either. Deterministic ordering so a rebuild is diffable.
        """
        by_tool: Dict[str, Dict[str, List[Example]]] = {}
        for example in self.trainable:
            slot = by_tool.setdefault(example.tool, {"a": [], "r": []})
            slot["a" if example.label is Label.ACCEPTED else "r"].append(example)

        pairs: List[Tuple[Example, Example]] = []
        for tool in sorted(by_tool):
            accepted = sorted(by_tool[tool]["a"], key=lambda e: (e.saga_id, e.step))
            rejected = sorted(by_tool[tool]["r"], key=lambda e: (e.saga_id, e.step))
            for good, bad in zip(accepted, rejected):
                pairs.append((good, bad))
        return tuple(pairs)

    def describe(self) -> dict:
        return {
            "sagas": self.sagas, "examples": len(self.examples),
            "counts": self.counts, "trainable": len(self.trainable),
            "with_context": len(self.with_context),
            "preference_pairs": len(self.preference_pairs()),
            "gate_refusals": list(self.gate_refusals),
        }

    def format_text(self) -> str:
        counts = self.counts
        lines = [f"corpus from {self.sagas} saga(s): {len(self.examples)} example(s)"]
        for label in Label:
            lines.append(f"  {label.value:<11} {counts[label.value]}")
        lines.append("")
        lines.append(f"  trainable            : {len(self.trainable)}  "
                     f"(accepted + rejected only)")
        lines.append(f"  with linked context  : {len(self.with_context)}")
        lines.append(f"  preference pairs     : {len(self.preference_pairs())}")
        if self.gate_refusals:
            lines.append(f"  gate-refused sagas   : {len(self.gate_refusals)}  "
                         f"(no step was logged for the refused call)")
        if counts[Label.COLLATERAL.value]:
            lines.append("")
            lines.append(f"  {counts[Label.COLLATERAL.value]} step(s) were rolled "
                         f"back only because a LATER step failed. Those actions")
            lines.append(f"  were correct and are excluded -- training on them would")
            lines.append(f"  teach the model to avoid the calls that worked.")
        if not self.trainable:
            lines.append("")
            lines.append("  Nothing trainable here. A log of sagas that all "
                         "completed has no")
            lines.append("  negative examples, and a log of nothing but failures "
                         "has no positives.")
        return "\n".join(lines)

    def to_jsonl(self, path, *, redact: Sequence[str] = (),
                 i_understand_this_contains_real_data: bool = False) -> int:
        """Write the trainable examples as JSONL. Returns the count written.

        The acknowledgement argument is deliberately awkward. This file will
        contain real tool arguments -- amounts, addresses, customer ids -- and a
        training corpus tends to travel further than the system that produced
        it. Naming the risk at the call site is cheaper than discovering it in a
        model's output later.
        """
        if not i_understand_this_contains_real_data:
            raise PermissionError(
                "refusing to write a corpus without acknowledgement. This file "
                "will contain real arguments from your WAL (amounts, addresses, "
                "customer ids) and training data outlives the system that made "
                "it. Pass redact=[...] for the sensitive fields and "
                "i_understand_this_contains_real_data=True, or synthesise a "
                "corpus with the same shape instead of shipping this one.")

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with open(target, "w", encoding="utf-8", newline="\n") as handle:
            for example in self.trainable:
                handle.write(json.dumps(example.to_json(redact=redact),
                                        sort_keys=True) + "\n")
                written += 1
        logger.info("wrote %d trainable example(s) to %s", written, target)
        return written


def build_corpus(records: Sequence[Mapping[str, Any]], *,
                 sagas: Optional[Iterable[str]] = None) -> Corpus:
    """Derive labelled examples from WAL records.

    Pass `sagas` to restrict to specific ids; by default every saga in the log
    is considered.
    """
    by_saga: Dict[str, List[Mapping[str, Any]]] = {}
    for record in records:
        if not isinstance(record, Mapping):
            continue
        saga_id = record.get("saga_id")
        if not isinstance(saga_id, str) or not saga_id:
            continue
        by_saga.setdefault(saga_id, []).append(record)

    wanted = set(sagas) if sagas is not None else set(by_saga)
    examples: List[Example] = []
    refusals: List[str] = []
    counted = 0

    for saga_id in sorted(wanted & set(by_saga)):
        counted += 1
        examples.extend(_examples_for(saga_id, by_saga[saga_id], refusals))

    return Corpus(examples=tuple(examples), sagas=counted,
                  gate_refusals=tuple(refusals))


def _examples_for(saga_id: str, records: Sequence[Mapping[str, Any]],
                  refusals: List[str]) -> List[Example]:
    intents: Dict[str, Mapping[str, Any]] = {}
    order: List[str] = []
    tools: Dict[str, str] = {}
    committed: Set[str] = set()
    unknown: Set[str] = set()
    compensated: Set[str] = set()
    orphaned: Set[str] = set()
    contexts: Dict[str, str] = {}         # tool -> context hash the model saw
    terminal: Optional[str] = None
    cause_type: Optional[str] = None

    for record in records:
        event = record.get("event")
        step_id = record.get("step_id")
        if event == "STEP_INTENT" and step_id:
            key = str(step_id)
            if key not in intents:
                order.append(key)
            intents[key] = dict(record.get("kwargs") or {})
            tools[key] = str(record.get("tool", "unknown"))
        elif event == "STEP_COMMITTED" and step_id:
            committed.add(str(step_id))
            tools.setdefault(str(step_id), str(record.get("tool", "unknown")))
        elif event == "STEP_UNKNOWN" and step_id:
            unknown.add(str(step_id))
            tools.setdefault(str(step_id), str(record.get("tool", "unknown")))
        elif event == "COMPENSATED" and step_id:
            compensated.add(str(step_id))
        elif event == "STEP_ORPHANED" and step_id:
            orphaned.add(str(step_id))
        elif event in ("SAGA_COMPLETE", "SAGA_ABORTED"):
            terminal = event
        elif event == "SAGA_ABORT_CAUSE":
            cause_type = str(record.get("cause_type") or "")
        elif event == "AGENT_DECISION":
            tool = record.get("tool")
            digest = record.get("context_hash")
            if isinstance(tool, str) and tool and isinstance(digest, str):
                contexts.setdefault(tool, digest)

    # The gate raises before STEP_INTENT, so a refused call has no step record.
    # Surface it at saga level rather than fabricate an example for it.
    if cause_type in ("PreFlightViolation", "ProvenanceViolation",
                      "CostBudgetExceeded"):
        refusals.append(f"{saga_id}: aborted by {cause_type}")

    out: List[Example] = []
    for index, key in enumerate(order, start=1):
        tool = tools.get(key, "unknown")
        label, reason = _label(key, terminal, committed, unknown, compensated)
        out.append(Example(
            saga_id=saga_id, step=index, tool=tool,
            arguments=intents.get(key, {}), label=label, reason=reason,
            context_hash=contexts.get(tool), orphaned=key in orphaned))
    return out


def _label(step_id: str, terminal: Optional[str], committed: Set[str],
           unknown: Set[str], compensated: Set[str]) -> Tuple[Label, str]:
    """Attribute the outcome to the step that earned it.

    The ordering of these branches is the whole correctness argument, so it is
    written out rather than compressed: UNKNOWN first (it is genuinely
    uninformative and must not be read as either success or failure), then the
    step that was rolled back (correct, undone for someone else), then a plain
    commit judged by how its saga ended.
    """
    if step_id in unknown:
        return (Label.AMBIGUOUS,
                "outcome UNKNOWN: the call raised or timed out and may still "
                "have landed, so it says nothing about whether it was right")

    if step_id in committed and step_id in compensated:
        return (Label.COLLATERAL,
                "committed, then rolled back because a later step failed -- the "
                "action itself was accepted at the time")

    if step_id in committed:
        if terminal == "SAGA_COMPLETE":
            return (Label.ACCEPTED, "committed in a saga that completed")
        if terminal == "SAGA_ABORTED":
            return (Label.COLLATERAL,
                    "committed in a saga that aborted, with no compensation "
                    "recorded for it -- not evidence the call was wrong")
        return (Label.AMBIGUOUS,
                "committed, but the saga has no terminal record: the process "
                "died before the outcome was known")

    return (Label.REJECTED,
            "intent was written and the step never committed: this is the call "
            "that failed")


def _redact(arguments: Mapping[str, Any], fields: Sequence[str]) -> Dict[str, Any]:
    """Drop named fields, matching on substring so `email` covers
    `customer_email`. Redaction is a blunt instrument by design -- a clever one
    that missed a field would be worse than an obvious one."""
    if not fields:
        return dict(arguments)
    lowered = [f.lower() for f in fields]
    return {
        key: (_REDACTED if any(f in str(key).lower() for f in lowered) else value)
        for key, value in arguments.items()
    }
