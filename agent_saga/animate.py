"""Animated SVG export: a rollback you can watch, from a log you can verify.

`graph.py` draws an executed saga as a static flowchart. That is the right
instrument for a code review and the wrong one for the two moments people
actually need it: an incident write-up, and the first ten seconds someone
spends deciding whether this library does anything real. A flowchart of a
rollback shows the *shape* of the unwind. It cannot show the thing that makes
the unwind surprising -- that the undo runs **backwards**, one step at a time,
after the forward path has already finished and failed.

This module renders that motion. Input is the same WAL the rest of the project
reads; output is one self-contained SVG file.

Constraints, and why each one is not negotiable:

  1. **No JavaScript, no network, no dependencies.** The output is animated with
     CSS `@keyframes` embedded in the document. That is what makes it valid
     inside `<img src="...">`, in a GitHub comment, in a PDF postmortem, and in
     a Content-Security-Policy'd page that blocks inline script. An animation
     that only plays in a live app is a demo; this is an artifact you can
     attach to a ticket.

  2. **The animation is a recording, not an illustration.** Every frame is
     derived from `graph._reconstruct` -- the *same* fold the static exporter
     uses. The two renderers cannot disagree about what happened, because there
     is one reconstruction and they both consume it. Nothing here invents a
     step, a timing, or an outcome that is not in the records.

  3. **A partial rollback must never animate like a clean one.** Compensated,
     compensation-failed, and orphaned get three colours, three labels, and a
     verdict banner that counts them. A committed step that a rollback never
     accounted for is reported as *unaccounted*, not quietly drawn as fine.
     Motion is persuasive, which is exactly why it must not flatter -- a
     prettier lie is a worse lie.

  4. **User data never becomes markup.** Tool names and saga names are
     attacker-influenced (an agent names its own tools). All user text is XML
     text-node-escaped and length-cropped; element ids are synthetic.

  5. **Deterministic bytes.** Same records, same output -- no timestamps, no
     randomness, no dict-order dependence. The SVG can be committed, diffed in
     review, and regenerated in CI to prove the picture still matches the log.

  6. **Motion is opt-out at the viewer's OS level.** The document carries a
     `prefers-reduced-motion: reduce` block that disables every animation and
     renders the final state. Someone whose vestibular system objects to
     sweeping motion still gets the full information, immediately.

    from agent_saga.animate import wal_to_animated_svg
    open("rollback.svg", "w").write(wal_to_animated_svg(await wal.read_all()))

or from the shell:

    agent-saga animate --wal ./agent-saga.wal --output rollback.svg
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple

from .graph import _crop, _reconstruct, _Step

__all__ = ["THEMES", "wal_to_animated_svg"]


# -- palette -----------------------------------------------------------------

THEMES: Dict[str, Dict[str, str]] = {
    "dark": {
        "bg": "#0b0f19", "panel": "#121826", "row": "#161d2e",
        "border": "#1f2937", "text": "#e5e7eb", "muted": "#8b9bb4",
        "grid": "#1a2231",
    },
    "light": {
        "bg": "#ffffff", "panel": "#f8fafc", "row": "#f1f5f9",
        "border": "#e2e8f0", "text": "#0f172a", "muted": "#64748b",
        "grid": "#eef2f7",
    },
}

# Semantics is author-declared, so it is safe to colour: it says what the author
# promised about undo, not what this module guessed.
_SEMANTICS_COLOUR = {
    "REVERSIBLE": "#3b82f6",
    "COMPENSABLE": "#f59e0b",
    "IRREVERSIBLE": "#ef4444",
    "PREVIEW": "#8b5cf6",
}
_SEMANTICS_FALLBACK = "#64748b"

# The three rollback outcomes the whole exporter exists to keep apart.
_ROLLBACK_COLOUR = {
    "compensated": "#10b981",
    "compensation_failed": "#ef4444",
    "orphaned": "#f59e0b",
}
_ROLLBACK_LABEL = {
    "compensated": "COMPENSATED",
    "compensation_failed": "COMPENSATION FAILED",
    "orphaned": "ORPHANED - no undo",
}

_FORWARD_LABEL = {
    "intent": "intent logged",
    "committed": "committed",
    "unknown": "UNKNOWN - may have landed",
    "fallback": "completed via fallback",
}
_FORWARD_COLOUR = {
    "intent": "#64748b",
    "committed": "#38bdf8",
    "unknown": "#f59e0b",
    "fallback": "#8b5cf6",
}

# -- geometry ----------------------------------------------------------------

_PAD = 24
_HEADER_H = 92
_ROW_H = 54
_ROW_GAP = 8
_PHASE_H = 30
_VERDICT_H = 66
_MAX_ROWS = 12
"""Beyond this the rows stop being legible and the file stops being small.
Extra steps are summarised in one honest row rather than silently dropped."""

_FONT = ("ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, "
         "'Liberation Mono', monospace")


def _esc(text: str) -> str:
    """Escape one untrusted fragment for an XML text node or attribute."""
    out = _crop(text)
    out = out.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return out.replace('"', "&quot;").replace("'", "&apos;")


def _fit(text: str, limit: int) -> str:
    """Crop to a character budget. The SVG has no text layout engine, so an
    over-long tool name would draw straight through the pill beside it."""
    text = _crop(text)
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"


def _num(value: float) -> str:
    """Format a coordinate deterministically, without float noise in the bytes."""
    rounded = round(float(value), 2)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:g}"


# -- verdict -----------------------------------------------------------------

def _verdict(steps: List[_Step], meta: Mapping[str, Any]) -> Tuple[str, str, str]:
    """Return `(headline, detail, colour)` for the banner.

    This is the `RollbackReport.clean` doctrine rendered as one line of text,
    and it is the single place in this module where getting it wrong would
    matter most: an animation that ends on a green "CLEAN" over a log
    containing an orphan is precisely the failure the library exists to make
    impossible elsewhere.
    """
    if not meta.get("rolled_back"):
        if meta.get("terminal") == "SAGA_COMPLETE":
            return ("SAGA COMPLETE", f"{len(steps)} step(s) committed, nothing to undo",
                    "#10b981")
        if meta.get("terminal") == "SAGA_ABORTED":
            cause = str(meta.get("cause") or "").strip()
            return ("SAGA ABORTED", cause or "aborted before any rollback ran", "#ef4444")
        return ("NO TERMINAL RECORD",
                "the process died or the log is truncated - state is unresolved",
                "#f59e0b")

    compensated = sum(1 for s in steps if s.rollback == "compensated")
    failed = sum(1 for s in steps if s.rollback == "compensation_failed")
    orphaned = sum(1 for s in steps if s.rollback == "orphaned")
    # A step that reached an effect but that the rollback never mentions is not
    # "fine by default". Say it is unaccounted for.
    unaccounted = sum(1 for s in steps
                      if s.rollback is None
                      and s.state in ("committed", "unknown", "fallback"))

    if failed or orphaned or unaccounted:
        parts = []
        if compensated:
            parts.append(f"{compensated} compensated")
        if failed:
            parts.append(f"{failed} compensation failed")
        if orphaned:
            parts.append(f"{orphaned} orphaned")
        if unaccounted:
            parts.append(f"{unaccounted} unaccounted for")
        colour = "#ef4444" if (failed or unaccounted) else "#f59e0b"
        return ("ROLLBACK INCOMPLETE", ", ".join(parts), colour)

    return ("ROLLBACK CLEAN",
            f"{compensated} step(s) undone in reverse order, nothing left behind",
            "#10b981")


# -- timeline ----------------------------------------------------------------

class _Keyframes:
    """Collects reveal times and emits one `@keyframes` rule per distinct time.

    Percent-based keyframes over a shared total duration are what make the
    animation loop seamlessly; per-element `animation-delay` cannot loop as a
    group. Times are deduplicated so a 12-step saga emits ~26 rules, not 60.
    """

    def __init__(self) -> None:
        self._times: Dict[Tuple[float, float, str], str] = {}
        self._order: List[Tuple[float, float, str]] = []

    def at(self, start: float, *, dur: float = 0.42, kind: str = "rise") -> str:
        key = (round(start, 3), round(dur, 3), kind)
        name = self._times.get(key)
        if name is None:
            name = f"a{len(self._order)}"
            self._times[key] = name
            self._order.append(key)
        return name

    def render(self, total: float, *, loop: bool) -> str:
        lines: List[str] = []
        iteration = "infinite" if loop else "1 forwards"
        for (start, dur, kind) in self._order:
            name = self._times[(start, dur, kind)]
            p0 = max(0.0, min(100.0, start / total * 100.0))
            p1 = max(p0, min(100.0, (start + dur) / total * 100.0))
            if kind == "rise":
                frm, to = "opacity:0;transform:translateY(7px)", "opacity:1;transform:translateY(0)"
            elif kind == "fade":
                frm, to = "opacity:0", "opacity:1"
            else:  # "flash" -- a brief attention pulse that returns to rest
                mid = min(100.0, (start + dur / 2) / total * 100.0)
                lines.append(
                    f".{name}{{animation:{name} {_num(total)}s linear {iteration};}}"
                    f"@keyframes {name}{{0%,{_num(p0)}%{{opacity:.25}}"
                    f"{_num(mid)}%{{opacity:1}}{_num(p1)}%,100%{{opacity:.25}}}}")
                continue
            lines.append(
                f".{name}{{animation:{name} {_num(total)}s linear {iteration};}}"
                f"@keyframes {name}{{0%,{_num(p0)}%{{{frm}}}"
                f"{_num(p1)}%,100%{{{to}}}}}")
        return "\n".join(lines)


# -- rendering ---------------------------------------------------------------

def wal_to_animated_svg(
    records: Any,
    *,
    title: Optional[str] = None,
    width: int = 900,
    theme: str = "dark",
    speed: float = 1.0,
    loop: bool = True,
) -> str:
    """Render an executed saga as a self-contained animated SVG.

    `speed` scales every duration (2.0 = twice as fast). `loop=False` plays
    once and holds the final frame -- the right choice for a PDF or a slide,
    where a restarting animation reads as a glitch.

    Never raises on a malformed log. An empty or unreadable one produces a
    valid SVG that says so, because a blank image and a broken export look the
    same to the person holding the ticket.
    """
    palette = THEMES.get(theme) or THEMES["dark"]
    steps, meta = _reconstruct(records)

    speed = max(0.1, min(10.0, float(speed) if speed else 1.0))
    width = max(420, min(2000, int(width)))

    shown = steps[:_MAX_ROWS]
    hidden = len(steps) - len(shown)
    rows = len(shown) + (1 if hidden else 0)

    rolled_back = bool(meta.get("rolled_back"))
    head, detail, verdict_colour = _verdict(steps, meta)

    # -- vertical layout ------------------------------------------------------
    y_phase = _PAD + _HEADER_H
    y_rows = y_phase + _PHASE_H
    rows_h = rows * (_ROW_H + _ROW_GAP) - _ROW_GAP if rows else 40
    y_verdict = y_rows + rows_h + 22
    height = y_verdict + _VERDICT_H + _PAD

    inner_x = _PAD + 16
    right_edge = width - _PAD - 16
    rb_w, sem_w = 196, 122
    rb_x = right_edge - rb_w
    sem_x = rb_x - 12 - sem_w
    name_budget = max(8, int((sem_x - (inner_x + 46) - 14) / 8.4))

    # -- timeline -------------------------------------------------------------
    kf = _Keyframes()
    t_head = 0.15 / speed
    step_gap = 0.40 / speed
    t_forward0 = 0.75 / speed
    t_forward_end = t_forward0 + max(rows, 1) * step_gap
    t_rollback0 = t_forward_end + 0.55 / speed
    t_rollback_end = t_rollback0 + (len(shown) * step_gap if rolled_back else 0.0)
    t_verdict = (t_rollback_end if rolled_back else t_forward_end) + 0.45 / speed
    total = t_verdict + 0.5 / speed + 2.4 / speed  # + hold, so the end is readable

    # The body is built first and the stylesheet composed afterwards: every
    # `@keyframes` rule is discovered *while* drawing, so emitting <style>
    # up-front would ship a document with no animations in it at all.
    out: List[str] = []
    add = out.append

    add(f'<rect width="{width}" height="{_num(height)}" rx="18" fill="{palette["bg"]}"/>')
    add(f'<rect x="1" y="1" width="{width - 2}" height="{_num(height - 2)}" rx="18" '
        f'fill="none" stroke="{palette["border"]}"/>')

    # -- header ---------------------------------------------------------------
    name = str(meta.get("name") or "").strip()
    saga_id = str(meta.get("saga_id") or "").strip()
    heading = title or (name or "saga")
    subtitle = saga_id or "no saga id in log"

    c_head = kf.at(t_head, kind="fade")
    add(f'<g class="{c_head}">')
    add(f'<text class="t" x="{inner_x}" y="{_PAD + 26}" font-size="20" '
        f'font-weight="700">{_esc(_fit(heading, 46))}</text>')
    add(f'<text class="m" x="{inner_x}" y="{_PAD + 50}" font-size="12">'
        f'{_esc(_fit(subtitle, 60))}</text>')
    add(f'<text class="m" x="{right_edge}" y="{_PAD + 26}" font-size="12" '
        f'text-anchor="end">agent-saga</text>')
    add("</g>")

    # Hash-chain motif: six linked blocks, pulsing in sequence. It is decoration,
    # and it is labelled as the one thing it depicts -- the chained log.
    chain_y = _PAD + 46
    chain_x0 = right_edge - 6 * 16 + 4
    for i in range(6):
        cls = kf.at(t_head + 0.09 * i / speed, dur=1.1 / speed, kind="flash")
        cx = chain_x0 + i * 16
        add(f'<rect class="{cls}" x="{_num(cx)}" y="{chain_y}" width="10" height="10" '
            f'rx="2.5" fill="#f59e0b"/>')
        if i < 5:
            add(f'<rect x="{_num(cx + 10)}" y="{chain_y + 4.5}" width="6" height="1" '
                f'fill="{palette["border"]}"/>')

    add(f'<rect x="{_PAD}" y="{_PAD + 66}" width="{width - 2 * _PAD}" height="1" '
        f'fill="{palette["grid"]}"/>')

    # -- phase label ----------------------------------------------------------
    c_fwd = kf.at(t_forward0 - 0.2 / speed, kind="fade")
    add(f'<text class="m {c_fwd}" x="{inner_x}" y="{y_phase + 12}" font-size="11" '
        f'letter-spacing="1.5">FORWARD PATH ↓</text>')
    if rolled_back:
        c_rb = kf.at(t_rollback0 - 0.25 / speed, kind="fade")
        add(f'<text class="{c_rb}" x="{right_edge}" y="{y_phase + 12}" font-size="11" '
            f'letter-spacing="1.5" text-anchor="end" fill="#ef4444">'
            f'↑ ROLLBACK, LIFO</text>')

    # -- rows -----------------------------------------------------------------
    if not shown:
        add(f'<rect x="{_PAD}" y="{y_rows}" width="{width - 2 * _PAD}" height="40" '
            f'rx="10" fill="{palette["row"]}"/>')
        add(f'<text class="m" x="{inner_x}" y="{y_rows + 20}" font-size="13">'
            f'no step records found in this log</text>')

    for i, step in enumerate(shown):
        y = y_rows + i * (_ROW_H + _ROW_GAP)
        sem = (step.semantics or "").upper()
        sem_colour = _SEMANTICS_COLOUR.get(sem, _SEMANTICS_FALLBACK)
        appear = kf.at(t_forward0 + i * step_gap)

        add(f'<g class="{appear}">')
        add(f'<rect x="{_PAD}" y="{_num(y)}" width="{width - 2 * _PAD}" '
            f'height="{_ROW_H}" rx="12" fill="{palette["row"]}" '
            f'stroke="{palette["border"]}"/>')
        add(f'<rect x="{_PAD + 10}" y="{_num(y + 12)}" width="4" height="{_ROW_H - 24}" '
            f'rx="2" fill="{sem_colour}"/>')
        add(f'<text x="{inner_x + 14}" y="{_num(y + _ROW_H / 2)}" font-size="12" '
            f'text-anchor="middle" fill="{palette["muted"]}">{i + 1}</text>')
        add(f'<text class="t" x="{inner_x + 34}" y="{_num(y + 21)}" font-size="14">'
            f'{_esc(_fit(step.tool, name_budget))}</text>')
        fwd_colour = _FORWARD_COLOUR.get(step.state, palette["muted"])
        add(f'<text x="{inner_x + 34}" y="{_num(y + 39)}" font-size="11" '
            f'fill="{fwd_colour}">'
            f'{_esc(_FORWARD_LABEL.get(step.state, step.state))}</text>')
        add(f'<rect x="{_num(sem_x)}" y="{_num(y + 15)}" width="{sem_w}" height="24" '
            f'rx="12" fill="none" stroke="{sem_colour}"/>')
        add(f'<text x="{_num(sem_x + sem_w / 2)}" y="{_num(y + 27)}" font-size="10" '
            f'text-anchor="middle" fill="{sem_colour}" letter-spacing="0.5">'
            f'{_esc(sem or "UNDECLARED")}</text>')
        add("</g>")

        # Rollback overlay. Revealed in reverse order -- the point of the whole
        # animation is that the undo runs backwards.
        if rolled_back and step.rollback:
            reveal = t_rollback0 + (len(shown) - 1 - i) * step_gap
            cls = kf.at(reveal)
            colour = _ROLLBACK_COLOUR.get(step.rollback, palette["muted"])
            label = _ROLLBACK_LABEL.get(step.rollback, step.rollback)
            add(f'<g class="{cls}">')
            add(f'<rect x="{_PAD + 10}" y="{_num(y + 12)}" width="4" '
                f'height="{_ROW_H - 24}" rx="2" fill="{colour}"/>')
            add(f'<rect x="{_num(rb_x)}" y="{_num(y + 15)}" width="{rb_w}" height="24" '
                f'rx="12" fill="{colour}" fill-opacity="0.14" stroke="{colour}"/>')
            add(f'<text x="{_num(rb_x + rb_w / 2)}" y="{_num(y + 27)}" font-size="10" '
                f'text-anchor="middle" fill="{colour}" letter-spacing="0.5">'
                f'{_esc(label)}</text>')
            add("</g>")
        elif rolled_back:
            # Committed, and the rollback never mentioned it. Draw the silence.
            reveal = t_rollback0 + (len(shown) - 1 - i) * step_gap
            cls = kf.at(reveal)
            add(f'<g class="{cls}">')
            add(f'<rect x="{_num(rb_x)}" y="{_num(y + 15)}" width="{rb_w}" height="24" '
                f'rx="12" fill="none" stroke="{palette["border"]}" '
                f'stroke-dasharray="3 3"/>')
            add(f'<text x="{_num(rb_x + rb_w / 2)}" y="{_num(y + 27)}" font-size="10" '
                f'text-anchor="middle" fill="{palette["muted"]}">no rollback record</text>')
            add("</g>")

    if hidden:
        y = y_rows + len(shown) * (_ROW_H + _ROW_GAP)
        cls = kf.at(t_forward0 + len(shown) * step_gap)
        add(f'<g class="{cls}">')
        add(f'<rect x="{_PAD}" y="{_num(y)}" width="{width - 2 * _PAD}" '
            f'height="{_ROW_H}" rx="12" fill="none" stroke="{palette["border"]}" '
            f'stroke-dasharray="4 4"/>')
        add(f'<text class="m" x="{inner_x}" y="{_num(y + _ROW_H / 2)}" font-size="12">'
            f'+ {hidden} more step(s) in this saga - see `agent-saga graph`</text>')
        add("</g>")

    # -- verdict --------------------------------------------------------------
    c_verdict = kf.at(t_verdict, dur=0.5 / speed)
    add(f'<g class="{c_verdict}">')
    add(f'<rect x="{_PAD}" y="{_num(y_verdict)}" width="{width - 2 * _PAD}" '
        f'height="{_VERDICT_H}" rx="14" fill="{verdict_colour}" fill-opacity="0.10" '
        f'stroke="{verdict_colour}"/>')
    add(f'<rect x="{_PAD}" y="{_num(y_verdict)}" width="4" height="{_VERDICT_H}" '
        f'rx="2" fill="{verdict_colour}"/>')
    add(f'<text x="{inner_x}" y="{_num(y_verdict + 24)}" font-size="15" '
        f'font-weight="700" fill="{verdict_colour}">{_esc(head)}</text>')
    add(f'<text class="m" x="{inner_x}" y="{_num(y_verdict + 45)}" font-size="12">'
        f'{_esc(_fit(detail, 78))}</text>')
    add("</g>")

    # -- document assembly ----------------------------------------------------
    head_lines: List[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {_num(height)}" '
        f'width="{width}" height="{_num(height)}" role="img" '
        f'aria-label="{_esc(head)}: {_esc(detail)}">',
        # Accessible name and description come first: a screen reader gets the
        # verdict without waiting for, or being able to perceive, the animation.
        f"<title>{_esc(title or 'agent-saga rollback')}</title>",
        f"<desc>{_esc(head)}. {_esc(detail)}.</desc>",
        "<style>",
        f"text{{font-family:{_FONT};dominant-baseline:middle}}",
        ".t{fill:%s}.m{fill:%s}" % (palette["text"], palette["muted"]),
        kf.render(total, loop=loop),
        # Constraint 6: the viewer's OS decides. Reduced motion gets the final
        # frame immediately, with every element at rest and fully visible.
        "@media (prefers-reduced-motion: reduce){"
        "*{animation:none !important;opacity:1 !important;transform:none !important}}",
        "</style>",
    ]
    return "\n".join(head_lines + out + ["</svg>"])
