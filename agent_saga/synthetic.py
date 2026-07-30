"""Synthetic WALs: the shape of your traffic, none of your customers' data.

Two problems share one answer.

**You cannot share a WAL.** It holds amounts, addresses, customer ids, message
bodies. So the log that would let a vendor reproduce your bug, or let CI run
against realistic traffic, or let an auditor rehearse a disclosure, is the one
artefact you are least able to hand over.

**You cannot test recovery at scale on fifty sagas.** The recovery daemon, the
certifier, the graph exporter and `prove_rollback` all behave differently at
fifty thousand, and nobody has fifty thousand failed transactions lying around.

So: learn the *shape* of a real log, and generate as much traffic as you like
from it.

    profile = WALProfile.fit(real_records)      # holds no real values
    records = synthesize(profile, sagas=50_000, seed=7)

**No real value is ever copied.** The profile keeps counts, transition
frequencies, length distributions and numeric ranges. It does not keep a single
string a customer typed. Strings are regenerated as `synthetic_<field>_<n>`
tokens; numbers are drawn from the observed range, not the observed values. That
is what makes the *profile itself* shareable -- fit it in production, ship the
profile, synthesise anywhere.

**Everything generated is marked, and that is not negotiable.** A synthetic
audit log indistinguishable from a real one is not a testing tool, it is an
instrument for fabricating evidence. Every record carries `__synthetic__: true`,
every saga id is prefixed `synthetic-`, and `is_synthetic()` lets any consumer
check in one call. This module will not produce output that could be mistaken
for a record of something that happened.

**The output is structurally valid**, so the tools it is meant to exercise
accept it for the right reasons: `SAGA_START` precedes its steps, `STEP_INTENT`
precedes `STEP_COMMITTED`, terminal events are consistent, and the hash chain is
stamped so `agent-saga verify` passes.

On CTGAN and friends: the argument distributions here are deliberately simple --
marginals and ranges, no learned joint distribution -- because this package
carries no required dependencies and torch is not a reasonable price for test
fixtures. If you want genuinely learned tabular synthesis, `value_synthesizer=`
takes a callable and a CTGAN model drops straight in. The structural modelling
(tool frequencies, first-order transitions, saga lengths, outcome rates) is the
part that matters for exercising the engine, and that is stdlib.
"""

from __future__ import annotations

import logging
import random
import statistics
from dataclasses import dataclass, field
from typing import (Any, Callable, Dict, Iterable, List, Mapping, Optional,
                    Sequence, Tuple)

logger = logging.getLogger("agent_saga.synthetic")

__all__ = [
    "SYNTHETIC_FIELD",
    "WALProfile",
    "is_synthetic",
    "synthesize",
]

SYNTHETIC_FIELD = "__synthetic__"
"""Present and true on every generated record. Checked by `is_synthetic`."""

SAGA_PREFIX = "synthetic-"

#: A value synthesizer takes (tool, field, kind, low, high) and returns a value.
#: Plug a CTGAN sampler in here; the default never reproduces a real value.
ValueSynthesizer = Callable[[str, str, str, Optional[float], Optional[float]], Any]


def is_synthetic(record_or_records: Any) -> bool:
    """True if this record -- or every record in this sequence -- is synthetic.

    Deliberately strict for a sequence: a log that is *mostly* synthetic is a
    log somebody mixed, and treating it as safe to share would be the exact
    mistake this module exists to prevent.
    """
    if isinstance(record_or_records, Mapping):
        return bool(record_or_records.get(SYNTHETIC_FIELD))
    try:
        records = list(record_or_records)
    except TypeError:
        return False
    return bool(records) and all(
        isinstance(r, Mapping) and r.get(SYNTHETIC_FIELD) for r in records)


@dataclass
class WALProfile:
    """The statistical shape of a log, carrying none of its contents.

    Safe to hand to anyone: there is no string here that a person typed, and no
    number that identifies a transaction. Fit it where the data is; synthesise
    where the data is not.
    """

    tools: Dict[str, int] = field(default_factory=dict)
    transitions: Dict[str, Dict[str, int]] = field(default_factory=dict)
    saga_lengths: List[int] = field(default_factory=list)
    semantics: Dict[str, str] = field(default_factory=dict)
    abort_rate: float = 0.0
    unknown_rate: float = 0.0
    #: "tool\x1ffield" -> ("number"|"string"|"bool", low, high)
    fields: Dict[str, Tuple[str, Optional[float], Optional[float]]] = \
        field(default_factory=dict)

    # -- fitting -----------------------------------------------------------------

    @classmethod
    def fit(cls, records: Sequence[Mapping[str, Any]],
            *, redact: Sequence[str] = ()) -> "WALProfile":
        """Learn the shape of a real log.

        Only aggregates are retained. A string value is reduced to the fact that
        the field held a string; a number to the range its field occupied.

        `redact` drops named fields entirely, matched on substring. Use it for
        numeric fields that are **identifiers rather than magnitudes** -- an
        account number, a card's last four, a customer id. Numeric synthesis
        preserves magnitude, not value: a generated number can coincide with a
        real one, and for a field whose values are all distinct identifiers that
        coincidence is a disclosure. A range is the right model for an amount
        and the wrong model for an account number, and only you know which is
        which.
        """
        profile = cls()
        dropped = [f.lower() for f in redact]
        by_saga: Dict[str, List[Mapping[str, Any]]] = {}
        for record in records:
            if not isinstance(record, Mapping):
                continue
            saga_id = record.get("saga_id")
            if isinstance(saga_id, str) and saga_id:
                by_saga.setdefault(saga_id, []).append(record)

        numeric: Dict[str, List[float]] = {}
        aborted = 0
        unknown_steps = 0
        total_steps = 0

        for saga_records in by_saga.values():
            sequence: List[str] = []
            for record in saga_records:
                event = record.get("event")
                if event == "STEP_INTENT":
                    tool = str(record.get("tool", "unknown"))
                    sequence.append(tool)
                    profile.tools[tool] = profile.tools.get(tool, 0) + 1
                    semantics = record.get("semantics")
                    if isinstance(semantics, str) and semantics:
                        profile.semantics.setdefault(tool, semantics)
                    for name, value in (record.get("kwargs") or {}).items():
                        if any(d in str(name).lower() for d in dropped):
                            continue          # declared an identifier; not modelled
                        _observe(profile, numeric, tool, str(name), value)
                elif event == "STEP_UNKNOWN":
                    unknown_steps += 1
                elif event == "SAGA_ABORTED":
                    aborted += 1

            total_steps += len(sequence)
            if sequence:
                profile.saga_lengths.append(len(sequence))
            for first, second in zip(sequence, sequence[1:]):
                profile.transitions.setdefault(first, {})
                profile.transitions[first][second] = \
                    profile.transitions[first].get(second, 0) + 1

        sagas = max(len(by_saga), 1)
        profile.abort_rate = aborted / sagas
        profile.unknown_rate = unknown_steps / max(total_steps, 1)

        for key, values in numeric.items():
            low, high = min(values), max(values)
            # Widen the range before storing it. A field with one observed value
            # would otherwise have low == high, and sampling from that interval
            # reproduces the real number exactly -- a certainty, not a chance.
            # Widening keeps the magnitude and stops the degenerate case being a
            # guaranteed disclosure.
            margin = max(abs(high - low) * 0.25, abs(high) * 0.1, 1.0)
            profile.fields[key] = ("number", low - margin, high + margin)
        return profile

    # -- introspection --------------------------------------------------------------

    @property
    def mean_length(self) -> float:
        return statistics.fmean(self.saga_lengths) if self.saga_lengths else 0.0

    def describe(self) -> dict:
        return {
            "tools": dict(sorted(self.tools.items())),
            "distinct_tools": len(self.tools),
            "mean_saga_length": round(self.mean_length, 2),
            "abort_rate": round(self.abort_rate, 4),
            "unknown_rate": round(self.unknown_rate, 4),
            "fields": {k: v[0] for k, v in sorted(self.fields.items())},
        }

    def format_text(self) -> str:
        lines = [f"WAL profile: {len(self.tools)} tool(s), "
                 f"mean saga length {self.mean_length:.1f}"]
        lines.append(f"  abort rate   : {self.abort_rate:.1%}")
        lines.append(f"  unknown rate : {self.unknown_rate:.1%}")
        for tool, count in sorted(self.tools.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {count:>6}x {tool} [{self.semantics.get(tool, '?')}]")
        if not self.tools:
            lines.append("  nothing observed -- this profile generates nothing")
        lines.append("")
        lines.append("  Contains no real values: safe to share.")
        return "\n".join(lines)


def synthesize(profile: WALProfile, *, sagas: int = 100, seed: Optional[int] = None,
               value_synthesizer: Optional[ValueSynthesizer] = None,
               chain: bool = True) -> List[Dict[str, Any]]:
    """Generate a structurally valid, unmistakably synthetic WAL.

    The *content* is deterministic for a given `seed`. The hash chain is not:
    `integrity.stamp` mixes a fresh random salt into every record on purpose, so
    a stamped log differs between runs even from the same seed. Pass
    `chain=False` for a byte-reproducible fixture, and `chain=True` (the
    default) when the log needs to satisfy `agent-saga verify`.
    """
    if sagas < 1:
        raise ValueError(f"sagas must be >= 1, got {sagas}")
    if not profile.tools:
        raise ValueError(
            "this profile observed no tools, so there is nothing to generate. "
            "Fit it against a log that contains at least one STEP_INTENT.")

    rng = random.Random(seed)
    synth = value_synthesizer or _default_value
    records: List[Dict[str, Any]] = []
    sequence = 0

    tool_names = sorted(profile.tools)
    tool_weights = [profile.tools[t] for t in tool_names]
    lengths = profile.saga_lengths or [1]

    for index in range(sagas):
        saga_id = f"{SAGA_PREFIX}{index:06d}"
        length = rng.choice(lengths)
        steps = _tool_sequence(profile, rng, tool_names, tool_weights, length)
        aborts = rng.random() < profile.abort_rate

        sequence += 1
        records.append(_record(sequence, "SAGA_START", saga_id,
                               name=f"synthetic-workload-{index % 7}"))

        committed: List[Tuple[str, str]] = []
        failed_at: Optional[int] = None

        for position, tool in enumerate(steps):
            step_id = f"{saga_id}-s{position}"
            kwargs = _arguments(profile, tool, rng, synth)

            sequence += 1
            records.append(_record(sequence, "STEP_INTENT", saga_id,
                                   step_id=step_id, tool=tool,
                                   semantics=profile.semantics.get(tool, "COMPENSABLE"),
                                   kwargs=kwargs))

            # An aborting saga fails at exactly one step, and every step before
            # it committed -- which is what a real abort looks like, and what
            # makes the generated log exercise the rollback path properly.
            is_failure = aborts and position == len(steps) - 1
            if is_failure or rng.random() < profile.unknown_rate:
                sequence += 1
                records.append(_record(sequence, "STEP_UNKNOWN", saga_id,
                                       step_id=step_id, tool=tool,
                                       semantics=profile.semantics.get(tool, "COMPENSABLE"),
                                       error="SyntheticFailure('generated')"))
                failed_at = position
                break

            sequence += 1
            records.append(_record(
                sequence, "STEP_COMMITTED", saga_id, step_id=step_id, tool=tool,
                semantics=profile.semantics.get(tool, "COMPENSABLE"),
                compensation={"handler": f"synthetic.undo_{tool.replace('.', '_')}",
                              "recoverable": True, "kwargs": {"step_id": step_id},
                              "description": f"synthetic inverse for {tool}"}))
            committed.append((step_id, tool))

        if failed_at is not None:
            sequence += 1
            records.append(_record(sequence, "SAGA_ABORT_CAUSE", saga_id,
                                   cause_type="SyntheticFailure",
                                   cause="generated failure"))
            sequence += 1
            records.append(_record(sequence, "ROLLBACK_START", saga_id,
                                   steps=len(committed)))
            for step_id, tool in reversed(committed):
                sequence += 1
                records.append(_record(sequence, "COMPENSATED", saga_id,
                                       step_id=step_id, tool=tool,
                                       idempotency_key=f"{step_id}-undo"))
            sequence += 1
            records.append(_record(sequence, "ROLLBACK_END", saga_id, clean=True))
            sequence += 1
            records.append(_record(sequence, "SAGA_ABORTED", saga_id))
        else:
            sequence += 1
            records.append(_record(sequence, "SAGA_COMPLETE", saga_id))

    if chain:
        # Stamp so `agent-saga verify` accepts it: a fixture that fails
        # integrity for the wrong reason teaches nothing.
        from .integrity import GENESIS, stamp_batch

        stamp_batch(records, GENESIS)
    return records


# -- internals ---------------------------------------------------------------------

def _record(sequence: int, event: str, saga_id: str, **payload: Any) -> Dict[str, Any]:
    """Every record is marked. See the module docstring on why that is not
    optional."""
    return {"seq": sequence, "event": event, "saga_id": saga_id,
            # A fixed timestamp base, not time.time(): a deterministic seed must
            # produce a deterministic log, and a wall clock would break that.
            "ts": 1_700_000_000.0 + sequence,
            SYNTHETIC_FIELD: True, **payload}


def _tool_sequence(profile: WALProfile, rng: random.Random,
                   tool_names: Sequence[str], weights: Sequence[int],
                   length: int) -> List[str]:
    """Walk the observed first-order transitions, falling back to the marginal
    when a tool has no recorded successor."""
    current = rng.choices(tool_names, weights=weights, k=1)[0]
    out = [current]
    while len(out) < length:
        following = profile.transitions.get(current)
        if following:
            options = sorted(following)
            current = rng.choices(options, weights=[following[o] for o in options],
                                  k=1)[0]
        else:
            current = rng.choices(tool_names, weights=weights, k=1)[0]
        out.append(current)
    return out


def _arguments(profile: WALProfile, tool: str, rng: random.Random,
               synth: ValueSynthesizer) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    prefix = f"{tool}\x1f"
    for key, (kind, low, high) in sorted(profile.fields.items()):
        if not key.startswith(prefix):
            continue
        name = key[len(prefix):]
        out[name] = synth(tool, name, kind, low, high)
    return out


def _default_value(tool: str, name: str, kind: str,
                   low: Optional[float], high: Optional[float]) -> Any:
    """Generate a value of the right *kind*, never a value that was observed.

    Strings become explicit synthetic tokens rather than plausible-looking
    fakes: a synthetic corpus that reads like real customer data invites exactly
    the confusion this module is built to avoid.
    """
    rng = random.Random(f"{tool}\x1f{name}")
    if kind == "number":
        span_low = 0.0 if low is None else float(low)
        span_high = span_low + 1.0 if high is None else float(high)
        value = rng.uniform(span_low, span_high)
        return int(round(value)) if float(value).is_integer() or span_high > 10 else value
    if kind == "bool":
        return rng.random() < 0.5
    return f"synthetic_{name}_{rng.randrange(1000):03d}"


def _observe(profile: WALProfile, numeric: Dict[str, List[float]],
             tool: str, name: str, value: Any) -> None:
    """Record what KIND a field held and, for numbers, its range. The value
    itself is deliberately dropped on the floor."""
    key = f"{tool}\x1f{name}"
    if isinstance(value, bool):
        profile.fields[key] = ("bool", None, None)
        return
    if isinstance(value, (int, float)):
        numeric.setdefault(key, []).append(float(value))
        profile.fields[key] = ("number", None, None)
        return
    profile.fields[key] = ("string", None, None)
