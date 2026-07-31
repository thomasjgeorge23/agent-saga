"""Whole-log visuals: the three pictures a WAL can draw that a list of lines cannot.

`animate.py` renders *one* saga's unwind. This module renders properties that
only exist across a whole log, and that a terminal genuinely cannot show:

  `fleet_timeline`  -- many sagas on real elapsed time. Concurrency, overlap,
                       and where in the wall clock the rollbacks landed.
  `chain_ribbon`    -- the hash chain, verified while drawing. A break is drawn
                       as a break.
  `outcome_matrix`  -- tool x outcome. Which calls the world kept, and which
                       ones had to be taken back.

Every constraint from `animate.py` carries over unchanged, and for the same
reasons -- self-contained CSS animation with no script and no network, so the
output survives `<img>`, a strict CSP, a GitHub comment and a PDF; user text
escaped and cropped, never markup; deterministic bytes so the file can be
committed and diffed; `prefers-reduced-motion` honoured inside the document.

Two of these have a stronger obligation than looking good:

**`chain_ribbon` verifies rather than decorates.** It recomputes the link
between each record and its predecessor. If `_ph` does not match the previous
`_h`, that block is drawn red and labelled BROKEN, and the header says the
chain is broken. A tamper-evidence graphic that drew a doctored log as intact
would be worse than no graphic at all -- it would be a forgery aid.

**`outcome_matrix` reports counts, not rates, below its support threshold.**
"100% rolled back" over a single observation is a number that means nothing and
reads as if it means everything. Cells under the threshold show the raw count
and are drawn muted. Same discipline `risk.py` applies to lift.

    from agent_saga.viz import chain_ribbon
    open("chain.svg", "w").write(chain_ribbon(await wal.read_all()))

or from the shell:

    agent-saga viz --wal ./agent-saga.wal --kind chain --output chain.svg
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple

from .animate import THEMES, _esc, _fit, _Keyframes, _num, _FONT
from .graph import _records, _text

__all__ = ["chain_ribbon", "fleet_timeline", "outcome_matrix"]

GENESIS = "0" * 64
"""The documented first `_ph`. A record claiming it is the head of a chain, not
a record whose predecessor is missing -- the two must not be confused, because
one is normal and the other is evidence."""

_PAD = 24

_OUTCOME_EVENTS = {
    "STEP_COMMITTED": ("kept", "#10b981"),
    "COMPLETED_VIA_FALLBACK": ("fallback", "#8b5cf6"),
    "STEP_UNKNOWN": ("unknown", "#f59e0b"),
    "COMPENSATED": ("undone", "#38bdf8"),
    "COMPENSATION_FAILED": ("undo failed", "#ef4444"),
    "STEP_ORPHANED": ("orphaned", "#f97316"),
}
_OUTCOME_ORDER = ["kept", "undone", "orphaned", "undo failed", "unknown", "fallback"]

MIN_SUPPORT = 5
"""Below this many observations a percentage is noise wearing a decimal point."""


def _open(width: int, height: float, palette: Mapping[str, str], *,
          label: str, desc: str, title: str) -> List[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {_num(height)}" '
        f'width="{width}" height="{_num(height)}" role="img" '
        f'aria-label="{_esc(label)}">',
        f"<title>{_esc(title)}</title><desc>{_esc(desc)}</desc>",
    ]


def _style(kf: _Keyframes, total: float, palette: Mapping[str, str],
           *, loop: bool) -> List[str]:
    return [
        "<style>",
        f"text{{font-family:{_FONT};dominant-baseline:middle}}",
        ".t{fill:%s}.m{fill:%s}" % (palette["text"], palette["muted"]),
        kf.render(total, loop=loop),
        "@media (prefers-reduced-motion: reduce){"
        "*{animation:none !important;opacity:1 !important;transform:none !important}}",
        "</style>",
    ]


def _frame(width: int, height: float, palette: Mapping[str, str]) -> List[str]:
    return [
        f'<rect width="{width}" height="{_num(height)}" rx="18" fill="{palette["bg"]}"/>',
        f'<rect x="1" y="1" width="{width - 2}" height="{_num(height - 2)}" rx="18" '
        f'fill="none" stroke="{palette["border"]}"/>',
    ]


def _header(x: int, right: int, heading: str, sub: str, palette: Mapping[str, str],
            cls: str, *, sub_colour: Optional[str] = None) -> List[str]:
    return [
        f'<g class="{cls}">',
        f'<text class="t" x="{x}" y="{_PAD + 24}" font-size="19" font-weight="700">'
        f'{_esc(_fit(heading, 52))}</text>',
        f'<text x="{x}" y="{_PAD + 47}" font-size="12" '
        f'fill="{sub_colour or palette["muted"]}">{_esc(_fit(sub, 96))}</text>',
        f'<text class="m" x="{right}" y="{_PAD + 24}" font-size="12" '
        f'text-anchor="end">agent-saga</text>',
        "</g>",
    ]


def _palette(theme: str) -> Dict[str, str]:
    return THEMES.get(theme) or THEMES["dark"]


# ============================================================================
# fleet timeline
# ============================================================================

def fleet_timeline(records: Any, *, width: int = 1000, theme: str = "dark",
                   speed: float = 1.0, loop: bool = True,
                   max_sagas: int = 14) -> str:
    """Draw every saga in the log as a bar on real elapsed time.

    This is the picture that shows what a log of concurrent sagas actually did:
    which ran at once, which overlapped, and where in the wall clock the
    rollbacks fell. A terminal shows the same records interleaved into one
    column, which is exactly the arrangement that hides it.

    Bars are coloured by terminal state, so a run that aborted is visible
    without reading a single label.
    """
    palette = _palette(theme)
    width = max(520, min(2400, int(width)))
    speed = max(0.1, min(10.0, float(speed) if speed else 1.0))

    sagas: Dict[str, Dict[str, Any]] = {}
    for record in _records(records):
        saga_id = record.get("saga_id")
        if not isinstance(saga_id, str) or not saga_id:
            continue
        ts = record.get("ts")
        if not isinstance(ts, (int, float)):
            continue
        entry = sagas.setdefault(saga_id, {
            "start": ts, "end": ts, "name": "", "state": "running",
            "rolled_back": False, "order": len(sagas)})
        entry["start"] = min(entry["start"], ts)
        entry["end"] = max(entry["end"], ts)
        event = record.get("event")
        if event == "SAGA_START":
            entry["name"] = _text(record.get("name"), "")
        elif event == "ROLLBACK_START":
            entry["rolled_back"] = True
        elif event == "SAGA_COMPLETE":
            entry["state"] = "complete"
        elif event == "SAGA_ABORTED":
            entry["state"] = "aborted"

    ordered = sorted(sagas.items(), key=lambda kv: (kv[1]["start"], kv[1]["order"]))
    hidden = max(0, len(ordered) - max_sagas)
    ordered = ordered[:max_sagas]

    row_h, row_gap = 34, 7
    y_rows = _PAD + 84
    rows_h = len(ordered) * (row_h + row_gap) - row_gap if ordered else 40
    height = y_rows + rows_h + (28 if hidden else 0) + _PAD + 34

    if ordered:
        t0 = min(e["start"] for _k, e in ordered)
        t1 = max(e["end"] for _k, e in ordered)
    else:
        t0 = t1 = 0.0
    span = max(1e-6, t1 - t0)

    label_w = 190
    track_x = _PAD + label_w
    track_w = width - track_x - _PAD - 12

    kf = _Keyframes()
    out: List[str] = []
    out += _frame(width, height, palette)

    aborted = sum(1 for _k, e in ordered if e["state"] == "aborted")
    heading = f"{len(sagas)} saga(s) over {span * 1000:.0f} ms"
    sub = (f"{aborted} aborted and rolled back"
           if aborted else "no rollbacks in this window")
    out += _header(_PAD + 16, width - _PAD - 16, heading, sub, palette,
                   kf.at(0.15 / speed, kind="fade"),
                   sub_colour="#ef4444" if aborted else None)

    # time axis
    axis_y = y_rows - 16
    for i in range(5):
        gx = track_x + track_w * i / 4
        out.append(f'<rect x="{_num(gx)}" y="{_num(axis_y)}" width="1" '
                   f'height="{_num(rows_h + 22)}" fill="{palette["grid"]}"/>')
        out.append(f'<text class="m" x="{_num(gx)}" y="{_num(axis_y - 6)}" '
                   f'font-size="10" text-anchor="middle">'
                   f'{_num(span * 1000 * i / 4)} ms</text>')

    if not ordered:
        out.append(f'<text class="m" x="{_PAD + 16}" y="{_num(y_rows + 20)}" '
                   f'font-size="13">no timestamped saga records in this log</text>')

    bar_colour = {"complete": "#10b981", "aborted": "#ef4444", "running": "#f59e0b"}
    for i, (saga_id, entry) in enumerate(ordered):
        y = y_rows + i * (row_h + row_gap)
        x0 = track_x + (entry["start"] - t0) / span * track_w
        x1 = track_x + (entry["end"] - t0) / span * track_w
        bar_w = max(3.0, x1 - x0)
        colour = bar_colour.get(entry["state"], palette["muted"])
        cls = kf.at(0.6 / speed + i * 0.13 / speed, dur=0.5 / speed, kind="fade")

        out.append(f'<text class="m" x="{_PAD + 16}" y="{_num(y + row_h / 2)}" '
                   f'font-size="11">'
                   f'{_esc(_fit(entry["name"] or saga_id, 22))}</text>')
        out.append(f'<rect x="{_num(track_x)}" y="{_num(y + 6)}" '
                   f'width="{_num(track_w)}" height="{row_h - 12}" rx="6" '
                   f'fill="{palette["row"]}"/>')
        out.append(f'<g class="{cls}">')
        out.append(f'<rect x="{_num(x0)}" y="{_num(y + 6)}" width="{_num(bar_w)}" '
                   f'height="{row_h - 12}" rx="6" fill="{colour}" fill-opacity="0.85"/>')
        if entry["rolled_back"]:
            # A hatch stripe, so the rolled-back span is distinguishable from a
            # merely-red bar in greyscale and for a red/green colour deficiency.
            out.append(f'<rect x="{_num(x0)}" y="{_num(y + 6)}" width="{_num(bar_w)}" '
                       f'height="3" fill="{palette["bg"]}" fill-opacity="0.55"/>')
        out.append("</g>")

    if hidden:
        out.append(f'<text class="m" x="{_PAD + 16}" '
                   f'y="{_num(y_rows + rows_h + 16)}" font-size="11">'
                   f'+ {hidden} more saga(s) not drawn</text>')

    legend_y = height - _PAD - 6
    for i, (name, colour) in enumerate((("complete", "#10b981"),
                                        ("aborted", "#ef4444"),
                                        ("no terminal record", "#f59e0b"))):
        lx = _PAD + 16 + i * 170
        out.append(f'<rect x="{_num(lx)}" y="{_num(legend_y - 5)}" width="10" '
                   f'height="10" rx="2" fill="{colour}"/>')
        out.append(f'<text class="m" x="{_num(lx + 16)}" y="{_num(legend_y)}" '
                   f'font-size="10">{name}</text>')

    total = 0.6 / speed + max(len(ordered), 1) * 0.13 / speed + 0.5 / speed + 2.4 / speed
    head = _open(width, height, palette, label=f"{heading}. {sub}",
                 desc=f"{heading}. {sub}.", title="agent-saga fleet timeline")
    return "\n".join(head + _style(kf, total, palette, loop=loop) + out + ["</svg>"])


# ============================================================================
# hash chain
# ============================================================================

def _verify_chain(records: List[Mapping[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    """Walk the chain, returning per-record link status and a break count.

    Deliberately does NOT recompute digests -- that is `integrity.verify`'s job
    and it needs the canonical serialisation. This checks the property a
    *picture* can honestly assert: that each record's recorded predecessor is
    the one that actually precedes it.
    """
    blocks: List[Dict[str, Any]] = []
    breaks = 0
    previous_hash: Optional[str] = None

    for record in records:
        digest = record.get("_h")
        parent = record.get("_ph")
        if not isinstance(digest, str):
            blocks.append({"digest": "", "status": "unchained",
                           "seq": record.get("seq"), "event": record.get("event")})
            continue

        if previous_hash is None:
            status = "head" if parent == GENESIS else "unknown-parent"
            if status == "unknown-parent":
                breaks += 1
        elif parent == previous_hash:
            status = "linked"
        else:
            status = "broken"
            breaks += 1

        blocks.append({"digest": digest, "status": status,
                       "seq": record.get("seq"), "event": record.get("event")})
        previous_hash = digest

    return blocks, breaks


def chain_ribbon(records: Any, *, width: int = 1000, theme: str = "dark",
                 speed: float = 1.0, loop: bool = True, blocks: int = 18) -> str:
    """Draw the WAL's hash chain, verifying each link while drawing it.

    A record whose `_ph` does not match its predecessor's `_h` is drawn red and
    labelled BROKEN, and the header states it. This graphic is allowed to look
    reassuring only when the chain it drew actually was.
    """
    palette = _palette(theme)
    width = max(520, min(2400, int(width)))
    speed = max(0.1, min(10.0, float(speed) if speed else 1.0))
    blocks = max(4, min(40, int(blocks)))

    parsed = _records(records)
    chain, breaks = _verify_chain(parsed)

    # Keep the head, and the tail, and every break -- a break must never be the
    # thing that got cropped out.
    if len(chain) > blocks:
        broken_idx = [i for i, b in enumerate(chain) if b["status"] in ("broken", "unknown-parent")]
        keep = set(range(min(3, len(chain))))
        keep |= set(range(max(0, len(chain) - 3), len(chain)))
        for i in broken_idx:
            keep |= {max(0, i - 1), i, min(len(chain) - 1, i + 1)}
        remaining = blocks - len(keep)
        if remaining > 0:
            step = max(1, len(chain) // max(1, remaining))
            keep |= set(range(0, len(chain), step))
        shown = [chain[i] for i in sorted(keep)][:blocks]
        elided = len(chain) - len(shown)
    else:
        shown, elided = chain, 0

    per_row = max(1, min(9, (width - 2 * _PAD - 32) // 104))
    rows = (len(shown) + per_row - 1) // per_row if shown else 1
    block_w, block_h, gap = 96, 78, 8
    y_blocks = _PAD + 86
    height = y_blocks + rows * (block_h + gap + 14) + _PAD + 12

    kf = _Keyframes()
    out: List[str] = []
    out += _frame(width, height, palette)

    # A record with no `_h` is not a link in a chain; it is a record outside
    # one. Counting those as "intact" would report the *absence* of tamper
    # evidence as the presence of it -- the single most dangerous thing this
    # graphic could say.
    chained = sum(1 for b in chain if b["status"] != "unchained")

    if breaks:
        heading = f"CHAIN BROKEN at {breaks} link(s)"
        sub = f"{len(chain)} record(s) read; the log has been altered or truncated"
        colour = "#ef4444"
    elif not chain or not chained:
        heading = "no chained records"
        sub = ("this log carries no _h/_ph fields - it was written with "
               "chain=False and is not tamper-evident")
        colour = "#f59e0b"
    elif chained < len(chain):
        heading = f"PARTIALLY CHAINED - {len(chain) - chained} record(s) unchained"
        sub = ("a log that is only sometimes chained is not evidence of "
               "anything; the unchained records carry no tamper protection")
        colour = "#f59e0b"
    else:
        heading = f"chain intact across {chained} record(s)"
        sub = "every record's recorded predecessor is the record that precedes it"
        colour = "#10b981"

    out += _header(_PAD + 16, width - _PAD - 16, heading, sub, palette,
                   kf.at(0.15 / speed, kind="fade"), sub_colour=colour)

    status_colour = {"linked": "#10b981", "head": "#38bdf8", "broken": "#ef4444",
                     "unknown-parent": "#ef4444", "unchained": palette["muted"]}

    for i, block in enumerate(shown):
        row, col = divmod(i, per_row)
        x = _PAD + 16 + col * (block_w + gap)
        y = y_blocks + row * (block_h + gap + 14)
        colour = status_colour.get(block["status"], palette["muted"])
        cls = kf.at(0.55 / speed + i * 0.08 / speed, dur=0.4 / speed)
        broken = block["status"] in ("broken", "unknown-parent")

        out.append(f'<g class="{cls}">')
        out.append(f'<rect x="{_num(x)}" y="{_num(y)}" width="{block_w}" '
                   f'height="{block_h}" rx="10" fill="{palette["row"]}" '
                   f'stroke="{colour}" stroke-width="{2 if broken else 1}"/>')
        out.append(f'<text class="m" x="{_num(x + 10)}" y="{_num(y + 16)}" '
                   f'font-size="9">#{_esc(str(block["seq"]))}</text>')
        digest = block["digest"] or "--------"
        out.append(f'<text x="{_num(x + 10)}" y="{_num(y + 36)}" font-size="11" '
                   f'fill="{colour}">{_esc(digest[:8])}</text>')
        out.append(f'<text class="m" x="{_num(x + 10)}" y="{_num(y + 52)}" '
                   f'font-size="8">{_esc(_fit(str(block["event"] or ""), 13))}</text>')
        if broken:
            out.append(f'<text x="{_num(x + 10)}" y="{_num(y + 68)}" font-size="9" '
                       f'font-weight="700" fill="#ef4444">BROKEN</text>')
        out.append("</g>")

        # link to the next block on the same row
        if col < per_row - 1 and i + 1 < len(shown):
            nxt = shown[i + 1]
            # The connector carries the *next* block's verdict: a break belongs
            # on the link that failed, not on the record after it.
            link_colour = ("#ef4444" if nxt["status"] in ("broken", "unknown-parent")
                           else palette["border"])
            out.append(f'<g class="{cls}"><rect x="{_num(x + block_w)}" '
                       f'y="{_num(y + block_h / 2 - 1)}" width="{gap}" height="2" '
                       f'fill="{link_colour}"/></g>')

    if elided:
        out.append(f'<text class="m" x="{_PAD + 16}" y="{_num(height - _PAD - 4)}" '
                   f'font-size="10">{elided} record(s) elided; every break is '
                   f'shown</text>')

    total = 0.55 / speed + max(len(shown), 1) * 0.08 / speed + 0.4 / speed + 2.4 / speed
    head = _open(width, height, palette, label=f"{heading}. {sub}",
                 desc=f"{heading}. {sub}.", title="agent-saga hash chain")
    return "\n".join(head + _style(kf, total, palette, loop=loop) + out + ["</svg>"])


# ============================================================================
# outcome matrix
# ============================================================================

def outcome_matrix(records: Any, *, width: int = 900, theme: str = "dark",
                   speed: float = 1.0, loop: bool = True,
                   max_tools: int = 10) -> str:
    """Tool x outcome. Which calls the world kept, and which had to be taken back.

    Percentages appear only where there is enough support to mean something;
    below `MIN_SUPPORT` observations a cell shows its raw count and is drawn
    muted. "100% orphaned" over one observation is a number that means nothing
    and reads as though it means everything.
    """
    palette = _palette(theme)
    width = max(560, min(2000, int(width)))
    speed = max(0.1, min(10.0, float(speed) if speed else 1.0))

    tallies: Dict[str, Dict[str, int]] = {}
    order: List[str] = []
    for record in _records(records):
        mapped = _OUTCOME_EVENTS.get(record.get("event"))
        if mapped is None:
            continue
        outcome, _colour = mapped
        tool = _text(record.get("tool"))
        row = tallies.get(tool)
        if row is None:
            row = tallies[tool] = {name: 0 for name in _OUTCOME_ORDER}
            order.append(tool)
        row[outcome] += 1

    ranked = sorted(order, key=lambda t: -sum(tallies[t].values()))
    hidden = max(0, len(ranked) - max_tools)
    ranked = ranked[:max_tools]

    label_w = 200
    cell_w = max(64, (width - 2 * _PAD - 32 - label_w) // len(_OUTCOME_ORDER))
    row_h = 40
    y_rows = _PAD + 108
    height = y_rows + max(len(ranked), 1) * (row_h + 6) + (24 if hidden else 0) + _PAD + 8

    kf = _Keyframes()
    out: List[str] = []
    out += _frame(width, height, palette)

    total_obs = sum(sum(r.values()) for r in tallies.values())
    undone = sum(r["undone"] + r["orphaned"] + r["undo failed"] for r in tallies.values())
    heading = f"{len(tallies)} tool(s), {total_obs} outcome(s)"
    sub = (f"{undone} effect(s) had to be taken back"
           if undone else "nothing in this log needed undoing")
    out += _header(_PAD + 16, width - _PAD - 16, heading, sub, palette,
                   kf.at(0.15 / speed, kind="fade"),
                   sub_colour="#f59e0b" if undone else None)

    x0 = _PAD + 16 + label_w
    for j, outcome in enumerate(_OUTCOME_ORDER):
        cx = x0 + j * cell_w + cell_w / 2
        out.append(f'<text class="m" x="{_num(cx)}" y="{_num(y_rows - 16)}" '
                   f'font-size="9" text-anchor="middle">{_esc(outcome)}</text>')

    if not ranked:
        out.append(f'<text class="m" x="{_PAD + 16}" y="{_num(y_rows + 20)}" '
                   f'font-size="13">no step outcomes in this log</text>')

    colour_of = {name: colour for name, colour in _OUTCOME_EVENTS.values()}
    for i, tool in enumerate(ranked):
        y = y_rows + i * (row_h + 6)
        row = tallies[tool]
        row_total = sum(row.values())
        cls = kf.at(0.6 / speed + i * 0.11 / speed, dur=0.45 / speed)

        out.append(f'<text class="t" x="{_PAD + 16}" y="{_num(y + row_h / 2)}" '
                   f'font-size="12">{_esc(_fit(tool, 24))}</text>')
        out.append(f'<g class="{cls}">')
        for j, outcome in enumerate(_OUTCOME_ORDER):
            count = row[outcome]
            cx = x0 + j * cell_w
            colour = colour_of.get(outcome, palette["muted"])
            # Opacity carries magnitude; the label carries the honest number.
            share = count / row_total if row_total else 0.0
            out.append(f'<rect x="{_num(cx + 2)}" y="{_num(y + 4)}" '
                       f'width="{cell_w - 4}" height="{row_h - 8}" rx="8" '
                       f'fill="{colour}" fill-opacity="{_num(0.08 + 0.55 * share)}"/>')
            if count:
                if row_total >= MIN_SUPPORT:
                    text, fill = f"{count} ({share * 100:.0f}%)", colour
                else:
                    text, fill = str(count), palette["muted"]
                out.append(f'<text x="{_num(cx + cell_w / 2)}" '
                           f'y="{_num(y + row_h / 2)}" font-size="10" '
                           f'text-anchor="middle" fill="{fill}">{text}</text>')
        out.append("</g>")

    if hidden:
        out.append(f'<text class="m" x="{_PAD + 16}" '
                   f'y="{_num(height - _PAD - 2)}" font-size="10">'
                   f'+ {hidden} more tool(s); showing the {max_tools} most active</text>')

    note = (f"percentages shown only at n>={MIN_SUPPORT}; smaller cells show the "
            f"raw count")
    out.append(f'<text class="m" x="{_PAD + 16}" y="{_PAD + 70}" font-size="10">'
               f'{_esc(note)}</text>')

    total = 0.6 / speed + max(len(ranked), 1) * 0.11 / speed + 0.45 / speed + 2.4 / speed
    head = _open(width, height, palette, label=f"{heading}. {sub}",
                 desc=f"{heading}. {sub}.", title="agent-saga outcome matrix")
    return "\n".join(head + _style(kf, total, palette, loop=loop) + out + ["</svg>"])
