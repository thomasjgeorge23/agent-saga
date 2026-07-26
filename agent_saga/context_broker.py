"""Tiered context with provenance receipts: compression that can prove itself.

Long-running agents drown in their own transcripts, and the standard fix --
summarize and forget -- silently converts an audit trail into hearsay. The
model acts on a summary; the summary drifted from the source; nobody can say
what the model actually saw. In a hospital or a bank that is not a quality
problem, it is a liability problem.

This broker applies the WAL doctrine to context:

  * **Three tiers.** HOT is verbatim recency (task frame, latest turns). WARM
    is summaries. COLD is full fidelity -- the original documents, addressable
    by span.
  * **Compression with a receipt.** A WARM summary is only admitted if every
    span it claims to compress resolves against COLD *right now* -- exact
    offsets, exact SHA-256 of the slice. At every pack, the receipts are
    re-verified: a summary whose source no longer matches is EVICTED and named
    in the pack report, never served. Compression becomes an auditable cache,
    not silent lossy truncation.
  * **Deterministic packing.** Same broker state + budget => byte-identical
    output. The stable prefix block never moves as HOT grows, so provider
    prompt caches keep hitting. Every pack reports what was included, what was
    excluded and why, and what was evicted -- and lands in the WAL as a
    ``CONTEXT_PACKED`` event carrying the output hash, so "what did the model
    see when it decided X?" is answerable from the log alone.

What this module does NOT claim: it does not make any model reason better,
and it does not summarize anything itself -- the caller (an LLM via
`router.Router`, a human, a heuristic) produces summary text; the broker's
job is to make that summary *accountable* to its sources or keep it out.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple, Union

__all__ = [
    "ColdStore",
    "ContextBroker",
    "FileColdStore",
    "MemoryColdStore",
    "PackOverflow",
    "PackedContext",
    "ProvenanceError",
    "Span",
]


def _hash_text(text: str) -> str:
    """Same convention as `universal._audit_signature`: a prefixed, canonical
    SHA-256 any third party can recompute from the recorded fields."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _approx_tokens(text: str) -> int:
    """chars/4 -- the same deliberately-crude estimate as `ir.approx_tokens`,
    used for budgeting only and labelled as an estimate everywhere it appears."""
    return math.ceil(len(text) / 4)


class ProvenanceError(RuntimeError):
    """A span does not resolve: missing document, out-of-range offsets, or a
    slice whose hash no longer matches the receipt. Raised loudly at admission
    or hydration; detected-and-evicted (never raised, never served) at pack."""


class PackOverflow(RuntimeError):
    """The mandatory prefix alone exceeds the budget. There is no honest pack
    to produce, so none is produced."""


@dataclass(frozen=True)
class Span:
    """A receipt for one slice of one COLD document: exact character offsets
    and the SHA-256 of exactly that slice."""

    doc_id: str
    start: int
    end: int            # slice semantics: [start, end)
    sha256: str

    def describe(self) -> dict:
        return {"doc_id": self.doc_id, "start": self.start,
                "end": self.end, "sha256": self.sha256}


class ColdStore(Protocol):
    """Full-fidelity document storage. Implementations must return exactly the
    text that was put -- the receipts hash exact slices of it."""

    def get(self, doc_id: str) -> Optional[str]: ...
    def put(self, doc_id: str, text: str) -> None: ...


class MemoryColdStore:
    def __init__(self) -> None:
        self._docs: Dict[str, str] = {}

    def get(self, doc_id: str) -> Optional[str]:
        return self._docs.get(doc_id)

    def put(self, doc_id: str, text: str) -> None:
        self._docs[doc_id] = text


class FileColdStore:
    """Disk-backed COLD tier for document packs that dwarf memory. Filenames
    are hashes of the doc_id -- no path traversal surface -- and writes are
    atomic (tmp + fsync + replace), the same discipline as everywhere else in
    this package."""

    def __init__(self, root: Union[str, Path]):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, doc_id: str) -> Path:
        return self.root / (hashlib.sha256(doc_id.encode("utf-8")).hexdigest()[:32] + ".txt")

    def get(self, doc_id: str) -> Optional[str]:
        p = self._path(doc_id)
        if not p.exists():
            return None
        return p.read_text("utf-8")

    def put(self, doc_id: str, text: str) -> None:
        target = self._path(doc_id)
        tmp = target.with_name(target.name + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)


@dataclass
class _WarmEntry:
    summary_id: str
    text: str
    spans: Tuple[Span, ...]
    doc_hashes: Mapping[str, str] = field(default_factory=dict)
    """Full-document hash of every referenced doc AT ADMISSION. The pack-time
    fast path: if a document's current hash equals this, every receipt into it
    was verified against byte-identical content and needs no per-slice work."""


@dataclass(frozen=True)
class PackedContext:
    """One pack, fully accounted for. `text` is what the model sees; the rest
    is the truthful report of how it came to be."""

    text: str
    budget_tokens: int
    estimated_tokens: int
    included: Tuple[str, ...]                 # entry ids, in packed order
    excluded: Mapping[str, str]               # entry id -> reason
    evicted: Mapping[str, str]                # summary id -> provenance failure
    content_hash: str                         # sha256 of `text`

    def describe(self) -> dict:
        return {
            "budget_tokens": self.budget_tokens,
            "estimated_tokens": self.estimated_tokens,
            "included": list(self.included),
            "excluded": dict(self.excluded),
            "evicted": dict(self.evicted),
            "content_hash": self.content_hash,
        }


class ContextBroker:
    """HOT / WARM / COLD context tiers with receipts, deterministic packing,
    and a WAL trail. Dependency-injected: bring any ColdStore, any WAL (or
    none -- everything still works, you just lose the audit trail and are told
    nothing of the sort silently: `wal=None` is your explicit choice)."""

    def __init__(
        self,
        *,
        prefix: str = "",
        cold: Optional[ColdStore] = None,
        wal: Any = None,
        max_hot_entries: int = 64,
        max_warm_entries: Optional[int] = None,
    ):
        if max_hot_entries < 1:
            raise ValueError(f"max_hot_entries must be >= 1, got {max_hot_entries}")
        if max_warm_entries is not None and max_warm_entries < 1:
            raise ValueError(f"max_warm_entries must be >= 1, got {max_warm_entries}")
        self.prefix = prefix
        """The byte-stable block every pack starts with (system prompt, task
        frame). Kept verbatim and first so provider prompt caches keep hitting
        as HOT grows behind it."""
        self.cold: ColdStore = cold if cold is not None else MemoryColdStore()
        self.wal = wal
        self._hot: List[Tuple[str, str]] = []          # (entry_id, text), oldest first
        self._warm: Dict[str, _WarmEntry] = {}         # insertion-ordered
        self._evictions: Dict[str, str] = {}           # summary_id -> reason (cumulative)
        self._ids = itertools.count(1)
        self._max_hot = max_hot_entries
        self._max_warm = max_warm_entries
        self.displaced: Dict[str, str] = {}
        """Summaries dropped for WARM capacity (oldest first), id -> reason.
        Capacity displacement is bookkeeping, not a provenance failure -- but a
        long-running agent's broker must be bounded, and what it forgot must be
        a readable fact, not a mystery."""

    # -- COLD ------------------------------------------------------------------

    def add_document(self, doc_id: str, text: str, *, chunk_chars: int = 4000) -> Tuple[Span, ...]:
        """Store a document at full fidelity and return receipt spans over it,
        chunked for summarization. The spans are the currency: whoever
        summarizes a chunk admits the summary *with* these receipts."""
        self.cold.put(doc_id, text)
        spans = []
        for start in range(0, max(len(text), 1), chunk_chars):
            end = min(start + chunk_chars, len(text))
            spans.append(Span(doc_id=doc_id, start=start, end=end,
                              sha256=_hash_text(text[start:end])))
        return tuple(spans)

    def hydrate(self, span: Span) -> str:
        """Fetch the exact source slice behind a receipt, verifying it. This is
        what a downstream gate calls when the model asserts something 'from the
        summary' and the assertion is about to cause a side effect."""
        return self._verify_span(span, self._fetch(span.doc_id))

    def _fetch(self, doc_id: str) -> str:
        text = self.cold.get(doc_id)
        if text is None:
            raise ProvenanceError(f"document {doc_id!r} is not in the cold store")
        return text

    @staticmethod
    def _verify_span(span: Span, text: str) -> str:
        if not (0 <= span.start <= span.end <= len(text)):
            raise ProvenanceError(
                f"span [{span.start}:{span.end}) is out of range for "
                f"{span.doc_id!r} (len={len(text)})")
        piece = text[span.start:span.end]
        digest = _hash_text(piece)
        if digest != span.sha256:
            raise ProvenanceError(
                f"slice of {span.doc_id!r} at [{span.start}:{span.end}) hashes to "
                f"{digest}, receipt says {span.sha256}; the source has changed "
                f"since the receipt was issued")
        return piece

    # -- WARM ------------------------------------------------------------------

    def admit_summary(self, text: str, spans: Sequence[Span]) -> str:
        """Admit a summary into WARM if and only if every receipt resolves
        right now. No receipts, no admission: a summary of nothing verifiable
        is an assertion, and assertions do not get to impersonate documents.

        Each referenced document is fetched ONCE (not once per span), and its
        full-content hash is recorded on the entry -- the pack-time fast path
        verifies a whole document with one comparison instead of re-slicing
        every receipt on every pack.
        """
        if not spans:
            raise ProvenanceError(
                "a summary must carry at least one span receipt; refusing to "
                "admit unverifiable text into the WARM tier")
        docs: Dict[str, str] = {}
        for span in spans:
            if span.doc_id not in docs:
                docs[span.doc_id] = self._fetch(span.doc_id)
            self._verify_span(span, docs[span.doc_id])

        summary_id = f"s{next(self._ids)}"
        self._warm[summary_id] = _WarmEntry(
            summary_id, text, tuple(spans),
            doc_hashes={doc_id: _hash_text(t) for doc_id, t in docs.items()})

        if self._max_warm is not None:
            while len(self._warm) > self._max_warm:
                oldest = next(iter(self._warm))
                del self._warm[oldest]
                self.displaced[oldest] = (
                    f"displaced: WARM capacity of {self._max_warm} reached by "
                    f"admission of {summary_id}")
        return summary_id

    # -- HOT -------------------------------------------------------------------

    def push_hot(self, text: str) -> str:
        """Append verbatim recency. Bounded: beyond `max_hot_entries`, the
        oldest entries fall off -- recency is the one tier where forgetting the
        old to keep the new is the correct bias."""
        entry_id = f"h{next(self._ids)}"
        self._hot.append((entry_id, text))
        if len(self._hot) > self._max_hot:
            self._hot = self._hot[-self._max_hot:]
        return entry_id

    def receipts(self, summary_id: str) -> Optional[Tuple[Span, ...]]:
        """The span receipts behind one live WARM summary, or None if it is not
        (or no longer) admitted. The grounding layer resolves citations here."""
        entry = self._warm.get(summary_id)
        return entry.spans if entry is not None else None

    def loss_reason(self, summary_id: str) -> Optional[str]:
        """Why a summary is gone, if it is: the provenance eviction or capacity
        displacement reason -- so a broken citation can say WHAT broke."""
        return self._evictions.get(summary_id) or self.displaced.get(summary_id)

    # -- introspection -----------------------------------------------------------

    def stats(self) -> dict:
        """Operational posture in one read: tier sizes plus everything this
        broker has ever evicted (provenance failures) or displaced (capacity).
        Cheap enough to expose on a health endpoint."""
        return {
            "hot_entries": len(self._hot),
            "warm_entries": len(self._warm),
            "evicted_total": len(self._evictions),
            "displaced_total": len(self.displaced),
            "max_hot_entries": self._max_hot,
            "max_warm_entries": self._max_warm,
        }

    # -- packing ---------------------------------------------------------------

    def pack(self, budget_tokens: int, *, extra_prefix: str = "") -> PackedContext:
        """Assemble the model's view. Deterministic: identical broker state,
        budget, and `extra_prefix` produce byte-identical output.

        `extra_prefix` lets a caller (e.g. an agent loop carrying its tool
        protocol) extend the stable block for its own packs without mutating
        this broker -- two callers can share one broker without corrupting
        each other's prefixes.

        Order and trimming policy (stated, because policy that isn't stated is
        policy that drifts): the prefix always leads and must fit or the pack
        refuses; WARM follows in admission order, each entry re-verified at
        this moment and evicted on any provenance failure; HOT fills the
        remainder newest-first-kept (the oldest HOT entries are the first
        dropped). Everything excluded or evicted is named, with its reason.

        Verification cost: each referenced document is fetched and hashed ONCE
        per pack. A document whose current hash equals the hash recorded at a
        summary's admission proves every receipt into it by one comparison;
        only documents that changed fall back to per-slice verification (which
        is how receipts into an append-only log survive tail growth). Tamper
        detection at every pack is the contract; O(spans x document) was not.
        """
        evicted_now: Dict[str, str] = {}
        doc_cache: Dict[str, Tuple[Optional[str], Optional[str]]] = {}  # id -> (text, hash)

        def fetch_once(doc_id: str) -> Tuple[Optional[str], Optional[str]]:
            if doc_id not in doc_cache:
                text = self.cold.get(doc_id)
                doc_cache[doc_id] = (text, _hash_text(text) if text is not None else None)
            return doc_cache[doc_id]

        for summary_id in list(self._warm):
            entry = self._warm[summary_id]
            try:
                for doc_id, admitted_hash in entry.doc_hashes.items():
                    text, current_hash = fetch_once(doc_id)
                    if text is None:
                        raise ProvenanceError(
                            f"document {doc_id!r} is not in the cold store")
                    if current_hash == admitted_hash:
                        continue                    # fast path: bytes identical
                    for span in entry.spans:        # changed doc: per-slice check
                        if span.doc_id == doc_id:
                            self._verify_span(span, text)
            except ProvenanceError as exc:
                reason = str(exc)
                evicted_now[summary_id] = reason
                self._evictions[summary_id] = reason
                del self._warm[summary_id]

        parts: List[str] = []
        included: List[str] = []
        excluded: Dict[str, str] = {}
        remaining = budget_tokens

        lead = (self.prefix + "\n\n" + extra_prefix if self.prefix and extra_prefix
                else self.prefix or extra_prefix)
        prefix_cost = _approx_tokens(lead) if lead else 0
        if prefix_cost > budget_tokens:
            raise PackOverflow(
                f"the mandatory prefix alone is ~{prefix_cost} tokens against a "
                f"budget of {budget_tokens}; refusing to emit a pack that "
                f"silently truncated its own ground rules")
        if lead:
            parts.append(lead)
            remaining -= prefix_cost

        for summary_id, entry in self._warm.items():
            sources = ",".join(f"{s.doc_id}@{s.start}-{s.end}" for s in entry.spans)
            block = f"--- summary {summary_id} (sources: {sources}) ---\n{entry.text}"
            cost = _approx_tokens(block)
            if cost > remaining:
                excluded[summary_id] = (
                    f"budget: needs ~{cost} tokens, {remaining} remained")
                continue
            parts.append(block)
            included.append(summary_id)
            remaining -= cost

        # HOT: keep the newest. Walk newest->oldest deciding, then emit the
        # keepers oldest->newest so the transcript reads forward.
        keep: List[Tuple[str, str]] = []
        for entry_id, text in reversed(self._hot):
            cost = _approx_tokens(text)
            if cost > remaining:
                excluded[entry_id] = (
                    f"budget: needs ~{cost} tokens, {remaining} remained "
                    f"(older HOT entries are dropped first)")
                continue
            keep.append((entry_id, text))
            remaining -= cost
        for entry_id, text in reversed(keep):
            parts.append(text)
            included.append(entry_id)

        text_out = "\n\n".join(parts)
        packed = PackedContext(
            text=text_out,
            budget_tokens=budget_tokens,
            estimated_tokens=budget_tokens - remaining,
            included=tuple(included),
            excluded=excluded,
            evicted=evicted_now,
            content_hash=_hash_text(text_out),
        )
        if self.wal is not None:
            self.wal.append("CONTEXT_PACKED", packed.describe())
        return packed
