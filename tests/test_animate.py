"""Tests for `agent_saga.animate`.

The load-bearing property is not "produces an SVG". It is that a partial
rollback can never animate like a clean one, that user text can never become
markup, and that the bytes are stable enough to commit and diff. Those are what
these tests are mostly about.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import pytest

from agent_saga.animate import THEMES, wal_to_animated_svg


# -- fixtures ----------------------------------------------------------------

def _saga(*, rolled_back: bool, steps, terminal="SAGA_COMPLETE", cause=None):
    records = [{"event": "SAGA_START", "saga_id": "s-1", "name": "checkout"}]
    for i, (tool, semantics, state) in enumerate(steps):
        records.append({"event": "STEP_INTENT", "saga_id": "s-1",
                        "step_id": f"st{i}", "tool": tool, "semantics": semantics})
        if state != "intent":
            event = {"committed": "STEP_COMMITTED", "unknown": "STEP_UNKNOWN",
                     "fallback": "COMPLETED_VIA_FALLBACK"}[state]
            records.append({"event": event, "saga_id": "s-1",
                            "step_id": f"st{i}", "tool": tool})
    if rolled_back:
        records.append({"event": "ROLLBACK_START", "saga_id": "s-1"})
    if cause:
        records.append({"event": "SAGA_ABORT_CAUSE", "saga_id": "s-1", "cause": cause})
    records.append({"event": terminal, "saga_id": "s-1"})
    return records


def _rollback(records, step_index, event, tool):
    records.insert(-1, {"event": event, "saga_id": "s-1",
                        "step_id": f"st{step_index}", "tool": tool})
    return records


THREE = [("stripe.charge", "COMPENSABLE", "committed"),
         ("inventory.reserve", "COMPENSABLE", "committed"),
         ("email.send", "IRREVERSIBLE", "committed")]


# -- it is a real SVG --------------------------------------------------------

def test_output_parses_as_xml():
    svg = wal_to_animated_svg(_saga(rolled_back=False, steps=THREE))
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg")
    assert root.get("viewBox")


def test_empty_log_still_renders_a_valid_svg_that_says_so():
    svg = wal_to_animated_svg([])
    ET.fromstring(svg)
    assert "no step records found" in svg


@pytest.mark.parametrize("garbage", [None, 42, "not a list", [None, 7, "x"],
                                     [{"event": None}, {"no_event": 1}]])
def test_malformed_input_never_raises(garbage):
    svg = wal_to_animated_svg(garbage)
    ET.fromstring(svg)


def test_no_script_and_no_external_reference():
    """The whole point of the format: safe to embed, works offline, survives CSP."""
    svg = wal_to_animated_svg(_saga(rolled_back=False, steps=THREE))
    # The SVG namespace declaration is a URI, not a fetch. Everything else that
    # looks like a URL would be.
    lowered = svg.lower().replace('xmlns="http://www.w3.org/2000/svg"', "")
    assert "<script" not in lowered
    assert "javascript:" not in lowered
    assert "http://" not in lowered
    assert "https://" not in lowered
    assert "xlink:href" not in lowered
    assert "@import" not in lowered
    assert "url(" not in lowered


def test_reduced_motion_block_is_present():
    svg = wal_to_animated_svg(_saga(rolled_back=False, steps=THREE))
    assert "prefers-reduced-motion: reduce" in svg
    assert "animation:none !important" in svg


# -- the honesty contract ----------------------------------------------------

def test_clean_rollback_says_clean():
    records = _saga(rolled_back=True, steps=THREE[:2], terminal="SAGA_ABORTED")
    _rollback(records, 0, "COMPENSATED", "stripe.charge")
    _rollback(records, 1, "COMPENSATED", "inventory.reserve")
    svg = wal_to_animated_svg(records)
    assert "ROLLBACK CLEAN" in svg
    assert "ROLLBACK INCOMPLETE" not in svg


def test_orphan_forbids_a_clean_verdict():
    records = _saga(rolled_back=True, steps=THREE, terminal="SAGA_ABORTED")
    _rollback(records, 0, "COMPENSATED", "stripe.charge")
    _rollback(records, 1, "COMPENSATED", "inventory.reserve")
    _rollback(records, 2, "STEP_ORPHANED", "email.send")
    svg = wal_to_animated_svg(records)
    assert "ROLLBACK INCOMPLETE" in svg
    assert "ROLLBACK CLEAN" not in svg
    assert "1 orphaned" in svg
    assert "ORPHANED" in svg


def test_failed_compensation_forbids_a_clean_verdict():
    records = _saga(rolled_back=True, steps=THREE[:2], terminal="SAGA_ABORTED")
    _rollback(records, 0, "COMPENSATION_FAILED", "stripe.charge")
    _rollback(records, 1, "COMPENSATED", "inventory.reserve")
    svg = wal_to_animated_svg(records)
    assert "ROLLBACK INCOMPLETE" in svg
    assert "1 compensation failed" in svg
    assert "COMPENSATION FAILED" in svg


def test_committed_step_with_no_rollback_record_is_reported_unaccounted():
    """Silence is not a clean bill of health. A rollback ran, this step reached
    an effect, and nothing in the log says what became of it."""
    records = _saga(rolled_back=True, steps=THREE[:2], terminal="SAGA_ABORTED")
    _rollback(records, 0, "COMPENSATED", "stripe.charge")
    svg = wal_to_animated_svg(records)
    assert "ROLLBACK INCOMPLETE" in svg
    assert "1 unaccounted for" in svg
    assert "no rollback record" in svg


def test_truncated_log_is_not_reported_as_success():
    records = [r for r in _saga(rolled_back=False, steps=THREE)
               if r["event"] != "SAGA_COMPLETE"]
    svg = wal_to_animated_svg(records)
    assert "NO TERMINAL RECORD" in svg
    assert "SAGA COMPLETE" not in svg


def test_unknown_outcome_is_labelled_as_maybe_landed():
    records = _saga(rolled_back=False, steps=[("api.call", "COMPENSABLE", "unknown")])
    svg = wal_to_animated_svg(records)
    assert "UNKNOWN - may have landed" in svg


def test_success_path_reports_complete_and_draws_no_rollback_lane():
    svg = wal_to_animated_svg(_saga(rolled_back=False, steps=THREE))
    assert "SAGA COMPLETE" in svg
    assert "ROLLBACK, LIFO" not in svg


# -- rollback really is animated in reverse ----------------------------------

def _delay_percent(svg: str, keyframe_name: str) -> float:
    """The start percentage of a keyframe -- when that element appears."""
    match = re.search(r"@keyframes %s\{0%%,([\d.]+)%%" % re.escape(keyframe_name), svg)
    assert match, f"no keyframe {keyframe_name} in output"
    return float(match.group(1))


def test_rollback_overlays_reveal_in_reverse_order():
    """LIFO is the single most surprising thing about a saga unwind, so the
    animation has to actually show the last step undone first."""
    records = _saga(rolled_back=True, steps=THREE, terminal="SAGA_ABORTED")
    _rollback(records, 0, "COMPENSATED", "stripe.charge")
    _rollback(records, 1, "COMPENSATED", "inventory.reserve")
    _rollback(records, 2, "COMPENSATED", "email.send")
    svg = wal_to_animated_svg(records)

    # Each row's rollback overlay carries the class of its reveal keyframe.
    overlays = re.findall(r'<g class="(a\d+)">\s*<rect x="34"', svg)
    assert len(overlays) == 3, overlays
    starts = [_delay_percent(svg, name) for name in overlays]
    # Row 1 is drawn first but must be revealed LAST.
    assert starts[0] > starts[1] > starts[2], starts


def test_forward_rows_reveal_in_execution_order():
    svg = wal_to_animated_svg(_saga(rolled_back=False, steps=THREE))
    # height="54" is the step-row height; it excludes the taller verdict banner,
    # which also starts at x="24".
    rows = re.findall(r'<g class="(a\d+)">\s*<rect x="24" y="(\d+)" width="\d+" '
                      r'height="54"', svg)
    assert len(rows) == 3, rows
    starts = [_delay_percent(svg, name) for name, _ in rows]
    assert starts[0] < starts[1] < starts[2], starts


# -- untrusted input never becomes markup ------------------------------------

@pytest.mark.parametrize("hostile", [
    '</text><script>alert(1)</script><text>',
    '"><animate onbegin="alert(1)"',
    "'; fill: url(http://evil/x); '",
    "<foreignObject><iframe src=http://evil>",
    "&#x3c;script&#x3e;",
])
def test_hostile_tool_name_cannot_inject(hostile):
    """The check is on the *parsed tree*, not the raw bytes. A tool named
    `onbegin="alert(1)"` should appear in the output -- as escaped text inside
    a <text> node, which is exactly the harmless outcome. What must never exist
    is an element that actually carries that attribute, or a script node."""
    records = _saga(rolled_back=False, steps=[(hostile, "COMPENSABLE", "committed")])
    svg = wal_to_animated_svg(records)
    root = ET.fromstring(svg)            # still well-formed

    tags, attrs = set(), set()
    for element in root.iter():
        tags.add(element.tag.rsplit("}", 1)[-1].lower())
        attrs.update(k.lower() for k in element.keys())

    assert not tags & {"script", "iframe", "foreignobject", "animate", "set", "a"}
    assert not any(a.startswith("on") for a in attrs), attrs
    assert not any("href" in a for a in attrs), attrs


def test_hostile_saga_name_cannot_inject():
    records = _saga(rolled_back=False, steps=THREE)
    records[0]["name"] = '"><script>alert(1)</script>'
    svg = wal_to_animated_svg(records)
    ET.fromstring(svg)
    assert "<script" not in svg.lower()


def test_long_tool_name_is_cropped_not_wrapped():
    records = _saga(rolled_back=False,
                    steps=[("x" * 500, "COMPENSABLE", "committed")])
    svg = wal_to_animated_svg(records)
    ET.fromstring(svg)
    assert "x" * 200 not in svg


# -- determinism and bounds --------------------------------------------------

def test_same_records_produce_identical_bytes():
    records = _saga(rolled_back=True, steps=THREE, terminal="SAGA_ABORTED")
    _rollback(records, 2, "STEP_ORPHANED", "email.send")
    assert wal_to_animated_svg(records) == wal_to_animated_svg(records)


def test_no_timestamp_leaks_into_the_output():
    """A file that changes every render cannot be committed or diffed."""
    records = _saga(rolled_back=False, steps=THREE)
    for r in records:
        r["ts"] = 1700000000.5
    first = wal_to_animated_svg(records)
    for r in records:
        r["ts"] = 1800000000.5
    assert wal_to_animated_svg(records) == first


def test_many_steps_are_summarised_not_dropped():
    steps = [(f"tool.{i}", "COMPENSABLE", "committed") for i in range(40)]
    svg = wal_to_animated_svg(_saga(rolled_back=False, steps=steps))
    ET.fromstring(svg)
    assert "more step(s) in this saga" in svg
    assert len(svg) < 40_000


@pytest.mark.parametrize("width,expected", [(10, 420), (5000, 2000), (900, 900)])
def test_width_is_clamped_to_something_renderable(width, expected):
    svg = wal_to_animated_svg(_saga(rolled_back=False, steps=THREE), width=width)
    assert ET.fromstring(svg).get("width") == str(expected)


@pytest.mark.parametrize("speed", [0.0, -5.0, 1000.0, None])
def test_absurd_speed_never_produces_a_broken_document(speed):
    svg = wal_to_animated_svg(_saga(rolled_back=False, steps=THREE), speed=speed)
    ET.fromstring(svg)
    # No zero or negative durations, which would freeze or invert the animation.
    for value in re.findall(r"animation:a\d+ (-?[\d.]+)s", svg):
        assert float(value) > 0, value


def test_loop_false_holds_the_final_frame():
    records = _saga(rolled_back=False, steps=THREE)
    assert "infinite" in wal_to_animated_svg(records, loop=True)
    once = wal_to_animated_svg(records, loop=False)
    assert "infinite" not in once
    assert "forwards" in once


@pytest.mark.parametrize("theme", sorted(THEMES))
def test_every_theme_renders(theme):
    svg = wal_to_animated_svg(_saga(rolled_back=False, steps=THREE), theme=theme)
    ET.fromstring(svg)
    assert THEMES[theme]["bg"] in svg


def test_unknown_theme_falls_back_rather_than_raising():
    svg = wal_to_animated_svg(_saga(rolled_back=False, steps=THREE), theme="chartreuse")
    ET.fromstring(svg)


# -- accessibility -----------------------------------------------------------

def test_verdict_is_available_without_seeing_the_animation():
    records = _saga(rolled_back=True, steps=THREE, terminal="SAGA_ABORTED")
    _rollback(records, 2, "STEP_ORPHANED", "email.send")
    svg = wal_to_animated_svg(records)
    root = ET.fromstring(svg)
    ns = "{http://www.w3.org/2000/svg}"
    desc = root.find(f"{ns}desc")
    assert desc is not None and "ROLLBACK INCOMPLETE" in (desc.text or "")
    assert "ROLLBACK INCOMPLETE" in (root.get("aria-label") or "")
    assert root.get("role") == "img"


# -- it agrees with the static exporter --------------------------------------

def test_animation_and_static_graph_read_the_same_log_the_same_way():
    """Both renderers share one reconstruction; this pins that they stay shared
    so a diagram and an animation of the same log can never disagree."""
    from agent_saga.graph import wal_to_mermaid

    records = _saga(rolled_back=True, steps=THREE, terminal="SAGA_ABORTED")
    _rollback(records, 0, "COMPENSATED", "stripe.charge")
    _rollback(records, 2, "STEP_ORPHANED", "email.send")

    svg = wal_to_animated_svg(records)
    mermaid = wal_to_mermaid(records)
    for tool in ("stripe.charge", "inventory.reserve", "email.send"):
        assert tool in svg and tool in mermaid
    # Both must surface the orphan.
    assert "ORPHANED" in svg and "ORPHANED" in mermaid


# -- CLI ---------------------------------------------------------------------

def test_cli_theme_choices_match_the_module():
    """The CLI hardcodes the theme names to keep its import cheap; this is the
    guard that stops the two lists drifting."""
    from agent_saga.cli import _ANIMATE_THEMES

    assert set(_ANIMATE_THEMES) == set(THEMES)


def test_cli_writes_a_file(tmp_path):
    import json

    from agent_saga.cli import main

    wal = tmp_path / "a.wal"
    records = _saga(rolled_back=True, steps=THREE, terminal="SAGA_ABORTED")
    _rollback(records, 2, "STEP_ORPHANED", "email.send")
    wal.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")

    out = tmp_path / "a.svg"
    assert main(["animate", "--wal", str(wal), "--output", str(out)]) == 0
    svg = out.read_text(encoding="utf-8")
    ET.fromstring(svg)
    assert "ROLLBACK INCOMPLETE" in svg


def test_cli_reports_a_missing_wal_rather_than_traceback(tmp_path):
    from agent_saga.cli import main

    assert main(["animate", "--wal", str(tmp_path / "nope.wal")]) == 2
