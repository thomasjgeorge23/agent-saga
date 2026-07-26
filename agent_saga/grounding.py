"""Grounded answers: a hallucination cannot pose as a sourced fact.

The enterprise problem with LLM chat is not that models are insufficiently
brilliant -- it is that a wrong answer and a right answer arrive in the same
confident voice, and nothing downstream can tell them apart. No middleware
makes a model hallucinate less. What a middleware CAN do, mechanically, is
make every claim in an answer wear its evidence or wear a label:

    VERIFIED           cites a live WARM summary whose receipts resolve right
                       now, and every direct quote appears in those sources
    UNCITED            the model's own assertion -- possibly true, possibly
                       hallucinated; explicitly labeled as unevidenced
    BROKEN_CITATION    cites a summary that was never admitted, was evicted
                       for provenance failure, or whose receipts no longer
                       resolve -- with the exact reason
    BROKEN_QUOTE       quotes text that does not appear in the cited sources
    UNSUPPORTED        an entailment hook (yours -- e.g. an LLM judge through
                       the Router) rejected the claim against its sources

That is the honest version of "anti-hallucination", and the claim is precise:
grounding does not stop a model from inventing things; it stops an invention
from masquerading as a sourced fact. A regulated buyer does not need every
sentence to be true -- they need every sentence to be *classifiable*, so
policy can act on it (block UNCITED numbers in financial reports, require
VERIFIED-only in clinical summaries, page a human on BROKEN_*).

Verification here is structural, not semantic: citations resolve, hashes
match, quotes appear in sources. Whether an un-quoted paraphrase *follows
from* its source is a judgment call -- that is what the optional `entailment`
hook is for, and this module stays honest by refusing to fake it built-in.

The citation protocol is what the model already sees: packed WARM blocks are
headed ``--- summary s3 (sources: doc@0-4000) ---``, so the model cites
``[s3]`` (or ``[s1,s2]``) inline. Small local models handle this fine -- it
is the same JSON-adjacent discipline the agent loop already demands.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .context_broker import ContextBroker, ProvenanceError, _hash_text

__all__ = [
    "Claim",
    "GroundedAnswer",
    "ground",
]

VERIFIED = "VERIFIED"
UNCITED = "UNCITED"
BROKEN_CITATION = "BROKEN_CITATION"
BROKEN_QUOTE = "BROKEN_QUOTE"
UNSUPPORTED = "UNSUPPORTED"

_CITATION = re.compile(r"\[(s\d+(?:\s*,\s*s\d+)*)\]")
_QUOTE = re.compile(r'"([^"]{12,})"')     # short quotes are idiom, not evidence
_SEGMENT = re.compile(r"(?<=[.!?])\s+|\n+")

#: An entailment judge: (claim_text, combined_source_text) -> bool. Yours to
#: supply -- an LLM judge routed through `Router`, an NLI model, a rulebook.
#: Absent, VERIFIED means structurally verified, and says so in `basis`.
Entailment = Callable[[str, str], bool]


@dataclass(frozen=True)
class Claim:
    text: str
    citations: Tuple[str, ...]
    status: str
    detail: str = ""

    def describe(self) -> dict:
        return {"text": self.text, "citations": list(self.citations),
                "status": self.status, "detail": self.detail}


@dataclass(frozen=True)
class GroundedAnswer:
    """One answer, every claim classified. The counts never flatter: an answer
    with one broken citation is an answer with a broken citation, whatever
    else it got right."""

    claims: Tuple[Claim, ...]
    basis: str                       # "structural" | "structural+entailment"
    content_hash: str                # sha256 of the raw answer text

    @property
    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for c in self.claims:
            out[c.status] = out.get(c.status, 0) + 1
        return out

    @property
    def fully_grounded(self) -> bool:
        """True only when every claim is VERIFIED. This is the bar a policy
        gate should read, and it is deliberately strict: 'mostly grounded' is
        a phrase for marketing, not for a gate."""
        return all(c.status == VERIFIED for c in self.claims)

    def describe(self) -> dict:
        return {"basis": self.basis, "content_hash": self.content_hash,
                "counts": self.counts,
                "claims": [c.describe() for c in self.claims]}

    def format_annotated(self) -> str:
        """The answer with its labels worn inline -- what a compliance reviewer
        reads instead of trusting the prose's tone of voice."""
        lines = []
        for c in self.claims:
            tag = c.status if c.status != VERIFIED else f"{VERIFIED}:{','.join(c.citations)}"
            lines.append(f"[{tag}] {c.text}")
        return "\n".join(lines)


def ground(
    answer_text: str,
    broker: ContextBroker,
    *,
    entailment: Optional[Entailment] = None,
) -> GroundedAnswer:
    """Classify every claim in `answer_text` against the broker's receipts.

    Never raises for a bad answer -- a bad answer is a *result*, expressed in
    the claim statuses; raising would let one broken citation hide the report
    on everything else. The result lands in the broker's WAL (if it has one)
    as an ``ANSWER_GROUNDED`` event, extending the audit chain to its final
    link: what the model saw, why it acted, and now what its answer could and
    could not prove.
    """
    claims: List[Claim] = []
    for segment in _split(answer_text):
        cited = _citations(segment)
        if not cited:
            claims.append(Claim(segment, (), UNCITED,
                                "no citation; the model's own assertion"))
            continue
        claims.append(_verify(segment, cited, broker, entailment))

    answer = GroundedAnswer(
        claims=tuple(claims),
        basis="structural+entailment" if entailment is not None else "structural",
        content_hash=_hash_text(answer_text),
    )
    if broker.wal is not None:
        broker.wal.append("ANSWER_GROUNDED", answer.describe())
    return answer


# -- internals -------------------------------------------------------------------

def _split(text: str) -> List[str]:
    """Sentence-ish segmentation. A heuristic and documented as one: segments
    are the unit citations bind to, not a linguistic truth claim."""
    return [s.strip() for s in _SEGMENT.split(text) if s.strip()]


def _citations(segment: str) -> Tuple[str, ...]:
    found: List[str] = []
    for group in _CITATION.findall(segment):
        found.extend(s.strip() for s in group.split(","))
    return tuple(dict.fromkeys(found))          # de-dupe, keep order


def _verify(segment: str, cited: Sequence[str], broker: ContextBroker,
            entailment: Optional[Entailment]) -> Claim:
    sources: List[str] = []
    for summary_id in cited:
        spans = broker.receipts(summary_id)
        if spans is None:
            reason = broker.loss_reason(summary_id)
            return Claim(segment, tuple(cited), BROKEN_CITATION,
                         f"cites {summary_id}, which is "
                         + (f"gone: {reason}" if reason else "not an admitted summary"))
        try:
            sources.extend(broker.hydrate(span) for span in spans)
        except ProvenanceError as exc:
            return Claim(segment, tuple(cited), BROKEN_CITATION,
                         f"receipts behind {summary_id} no longer resolve: {exc}")

    combined = "\n".join(sources)
    for quote in _QUOTE.findall(segment):
        if quote not in combined:
            return Claim(segment, tuple(cited), BROKEN_QUOTE,
                         f"quotes {quote[:60]!r}, which appears in none of the "
                         f"cited sources")

    if entailment is not None and not entailment(segment, combined):
        return Claim(segment, tuple(cited), UNSUPPORTED,
                     "the entailment hook rejected this claim against its sources")

    return Claim(segment, tuple(cited), VERIFIED)
