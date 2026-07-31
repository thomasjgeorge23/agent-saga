"""Tests for `agent_saga.viz`.

Three renderers, three obligations beyond "produces an SVG":

  `chain_ribbon`   must never draw a doctored log as intact, and must never
                   crop a break out of the picture.
  `outcome_matrix` must not print a percentage it does not have the support
                   for.
  `fleet_timeline` must not invent time it was not given.

Plus the constraints every renderer in this project shares: no script, no
network, deterministic bytes, hostile text escaped, reduced-motion honoured.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET

import pytest

from agent_saga.viz import (GENESIS, MIN_SUPPORT, chain_ribbon, fleet_timeline,
                            outcome_matrix)

RENDERERS = [fleet_timeline, chain_ribbon, outcome_matrix]


# -- fixtures ----------------------------------------------------------------

def _chained(n=8, *, base_ts=1700000000.0):
    """A well-formed hash-chained log."""
    records, previous = [], GENESIS
    for i in range(n):
        digest = f"{i:064x}"
        records.append({
            "seq": i + 1, "ts": base_ts + i * 0.01,
            "event": "STEP_COMMITTED" if i % 2 else "STEP_INTENT",
            "saga_id": "s-1", "name": "checkout", "tool": "stripe.charge",
            "_ph": previous, "_h": digest,
        })
        previous = digest
    return records


def _fleet(sagas=4):
    records = []
    for s in range(sagas):
        base = 1700000000.0 + s * 0.05
        records.append({"event": "SAGA_START", "saga_id": f"s-{s}",
                        "name": f"job-{s}", "ts": base})
        records.append({"event": "STEP_INTENT", "saga_id": f"s-{s}",
                        "tool": "api.call", "ts": base + 0.02})
        if s % 2:
            records.append({"event": "ROLLBACK_START", "saga_id": f"s-{s}",
                            "ts": base + 0.03})
            records.append({"event": "SAGA_ABORTED", "saga_id": f"s-{s}",
                            "ts": base + 0.04})
        else:
            records.append({"event": "SAGA_COMPLETE", "saga_id": f"s-{s}",
                            "ts": base + 0.04})
    return records


def _outcomes(tool="stripe.charge", **counts):
    event_for = {"kept": "STEP_COMMITTED", "undone": "COMPENSATED",
                 "orphaned": "STEP_ORPHANED", "undo failed": "COMPENSATION_FAILED",
                 "unknown": "STEP_UNKNOWN", "fallback": "COMPLETED_VIA_FALLBACK"}
    records = []
    for name, count in counts.items():
        event = event_for[name.replace("_", " ")]
        records += [{"event": event, "tool": tool, "saga_id": "s"}] * count
    return records


# -- shared constraints ------------------------------------------------------

@pytest.mark.parametrize("render", RENDERERS)
def test_output_parses_as_xml(render):
    root = ET.fromstring(render(_chained()))
    assert root.tag.endswith("svg")
    assert root.get("viewBox")


@pytest.mark.parametrize("render", RENDERERS)
@pytest.mark.parametrize("garbage", [None, 42, "not a list", [None, 7, "x"],
                                     [{"event": None}, {"no_event": 1}], []])
def test_malformed_input_never_raises(render, garbage):
    ET.fromstring(render(garbage))


@pytest.mark.parametrize("render", RENDERERS)
def test_no_script_and_no_external_reference(render):
    svg = render(_chained()).lower().replace(
        'xmlns="http://www.w3.org/2000/svg"', "")
    assert "<script" not in svg
    assert "javascript:" not in svg
    assert "http://" not in svg and "https://" not in svg
    assert "xlink:href" not in svg
    assert "@import" not in svg
    assert "url(" not in svg


@pytest.mark.parametrize("render", RENDERERS)
def test_reduced_motion_block_is_present(render):
    svg = render(_chained())
    assert "prefers-reduced-motion: reduce" in svg
    assert "animation:none !important" in svg


@pytest.mark.parametrize("render", RENDERERS)
def test_deterministic_bytes(render):
    records = _chained()
    assert render(records) == render(records)


@pytest.mark.parametrize("render", RENDERERS)
def test_accessible_name_and_description(render):
    root = ET.fromstring(render(_chained()))
    ns = "{http://www.w3.org/2000/svg}"
    assert root.get("role") == "img"
    assert root.get("aria-label")
    assert (root.find(f"{ns}desc").text or "").strip()


@pytest.mark.parametrize("render", RENDERERS)
@pytest.mark.parametrize("hostile", [
    '</text><script>alert(1)</script><text>',
    '"><animate onbegin="alert(1)"',
    "<foreignObject><iframe src=http://evil>",
])
def test_hostile_names_cannot_inject(render, hostile):
    records = _chained()
    for r in records:
        r["tool"] = hostile
        r["name"] = hostile
    root = ET.fromstring(render(records))

    tags, attrs = set(), set()
    for element in root.iter():
        tags.add(element.tag.rsplit("}", 1)[-1].lower())
        attrs.update(k.lower() for k in element.keys())
    assert not tags & {"script", "iframe", "foreignobject", "animate", "set", "a"}
    assert not any(a.startswith("on") for a in attrs), attrs
    assert not any("href" in a for a in attrs), attrs


@pytest.mark.parametrize("render", RENDERERS)
@pytest.mark.parametrize("speed", [0.0, -5.0, 1000.0, None])
def test_absurd_speed_never_produces_a_broken_document(render, speed):
    svg = render(_chained(), speed=speed)
    ET.fromstring(svg)
    for value in re.findall(r"animation:a\d+ (-?[\d.]+)s", svg):
        assert float(value) > 0, value


@pytest.mark.parametrize("render", RENDERERS)
def test_loop_false_holds_the_final_frame(render):
    records = _chained()
    assert "infinite" in render(records, loop=True)
    once = render(records, loop=False)
    assert "infinite" not in once and "forwards" in once


# -- chain_ribbon: the one that must not flatter -----------------------------

def test_intact_chain_is_reported_intact():
    svg = chain_ribbon(_chained())
    assert "chain intact" in svg
    assert "CHAIN BROKEN" not in svg
    assert "BROKEN" not in svg


def test_a_tampered_link_is_drawn_as_broken():
    records = _chained()
    records[4]["_ph"] = "f" * 64          # someone spliced a record out
    svg = chain_ribbon(records)
    assert "CHAIN BROKEN" in svg
    assert "BROKEN" in svg
    assert "chain intact" not in svg


def test_every_break_is_counted():
    records = _chained(10)
    records[3]["_ph"] = "a" * 64
    records[7]["_ph"] = "b" * 64
    svg = chain_ribbon(records)
    assert "CHAIN BROKEN at 2 link(s)" in svg


def test_a_break_is_never_the_thing_that_gets_cropped():
    """With more records than blocks, the elision must keep every break. A
    tamper-evidence graphic that cropped out the tamper would be a forgery aid."""
    records = _chained(120)
    records[60]["_ph"] = "c" * 64          # dead centre, far from head and tail
    svg = chain_ribbon(records, blocks=12)
    assert "BROKEN" in svg
    assert "CHAIN BROKEN at 1 link(s)" in svg


def test_first_record_not_at_genesis_is_a_break_not_a_head():
    """A log whose first record claims an unknown parent is a log with its
    beginning removed."""
    records = _chained()
    records[0]["_ph"] = "d" * 64
    svg = chain_ribbon(records)
    assert "CHAIN BROKEN" in svg


def test_genesis_start_is_a_head_not_a_break():
    svg = chain_ribbon(_chained())
    assert "CHAIN BROKEN" not in svg


def test_unchained_log_says_so_rather_than_claiming_intact():
    records = [{"seq": i, "event": "STEP_INTENT", "ts": 1.0 * i} for i in range(5)]
    svg = chain_ribbon(records)
    assert "no chained records" in svg
    assert "chain intact" not in svg


def test_chain_does_not_recompute_digests_it_cannot_verify():
    """The picture asserts link continuity, which it can check. It must not
    claim the stronger property (`agent-saga verify` recomputes digests over
    the canonical serialisation) -- so a log with consistent-but-wrong digests
    is reported as linked, and the wording says 'predecessor', not 'valid'."""
    svg = chain_ribbon(_chained())
    assert "predecessor" in svg
    assert "valid" not in svg.lower().replace("invalid", "")


# -- outcome_matrix: no percentage without support ---------------------------

def _cell_labels(svg: str) -> list:
    """Text nodes from the matrix body, excluding the CSS (whose keyframes are
    full of literal `%` signs) and the header."""
    return re.findall(r'<text[^>]*text-anchor="middle"[^>]*>([^<]*)</text>', svg)


def test_low_support_shows_a_count_not_a_percentage():
    records = _outcomes(orphaned=1)
    assert len(records) < MIN_SUPPORT
    labels = _cell_labels(outcome_matrix(records))
    assert "1" in labels
    assert not any("%" in label for label in labels), labels


def test_sufficient_support_shows_a_percentage():
    records = _outcomes(kept=MIN_SUPPORT + 5, orphaned=2)
    labels = _cell_labels(outcome_matrix(records))
    assert any("%" in label for label in labels), labels


def test_a_partially_chained_log_is_not_reported_as_intact():
    """Some records chained and some not is not a weaker guarantee; it is no
    guarantee, because the unchained ones can be edited freely."""
    # Chaining switched off partway -- the realistic shape. (Deleting a record
    # from the *middle* is a stronger fault and is correctly reported as
    # CHAIN BROKEN, because the surviving links genuinely no longer connect.)
    records = _chained(6) + [
        {"seq": 7, "ts": 1700000000.1, "event": "STEP_INTENT",
         "saga_id": "s-1", "tool": "stripe.charge"},
        {"seq": 8, "ts": 1700000000.2, "event": "STEP_COMMITTED",
         "saga_id": "s-1", "tool": "stripe.charge"},
    ]
    svg = chain_ribbon(records)
    assert "PARTIALLY CHAINED" in svg
    assert "chain intact" not in svg
    assert "CHAIN BROKEN" not in svg


def test_the_support_threshold_is_stated_on_the_chart():
    svg = outcome_matrix(_outcomes(kept=3))
    assert f"n&gt;={MIN_SUPPORT}" in svg or f"n>={MIN_SUPPORT}" in svg


def test_undone_effects_are_counted_in_the_header():
    svg = outcome_matrix(_outcomes(kept=4, undone=3, orphaned=2))
    assert "5 effect(s) had to be taken back" in svg


def test_a_log_with_nothing_undone_says_so():
    svg = outcome_matrix(_outcomes(kept=6))
    assert "nothing in this log needed undoing" in svg


def test_busiest_tools_are_kept_when_cropping():
    records = []
    for i in range(30):
        records += _outcomes(tool=f"tool.{i}", kept=1)
    records += _outcomes(tool="tool.busy", kept=50)
    svg = outcome_matrix(records, max_tools=5)
    assert "tool.busy" in svg
    assert "more tool(s)" in svg


# -- fleet_timeline: does not invent time ------------------------------------

def test_records_without_timestamps_are_not_placed_on_the_axis():
    records = [{"event": "SAGA_START", "saga_id": "s-1", "name": "x"}]
    svg = fleet_timeline(records)
    assert "no timestamped saga records" in svg


def test_aborted_sagas_are_counted_in_the_header():
    svg = fleet_timeline(_fleet(4))
    assert "2 aborted and rolled back" in svg


def test_clean_fleet_says_no_rollbacks():
    records = [r for r in _fleet(2) if r["event"] != "ROLLBACK_START"]
    records = [r for r in records if r["event"] != "SAGA_ABORTED"]
    svg = fleet_timeline(records)
    assert "no rollbacks in this window" in svg


def test_elapsed_span_comes_from_the_records():
    records = _fleet(2)
    svg = fleet_timeline(records)
    span_ms = (max(r["ts"] for r in records) - min(r["ts"] for r in records)) * 1000
    assert f"over {span_ms:.0f} ms" in svg


def test_many_sagas_are_summarised_not_dropped_silently():
    svg = fleet_timeline(_fleet(40), max_sagas=6)
    ET.fromstring(svg)
    assert "more saga(s) not drawn" in svg


def test_rollback_is_distinguishable_without_colour():
    """A red bar and a green bar are the same bar to a red/green colour
    deficiency, so a rolled-back span also carries a hatch stripe."""
    with_rollback = fleet_timeline(_fleet(2))
    without = fleet_timeline([r for r in _fleet(2)
                              if r["event"] not in ("ROLLBACK_START", "SAGA_ABORTED")])
    assert with_rollback.count('height="3"') > without.count('height="3"')


# -- CLI ---------------------------------------------------------------------

def test_cli_kind_choices_match_the_module():
    from agent_saga.cli import _VIZ_KINDS

    assert set(_VIZ_KINDS) == {"chain", "fleet", "outcomes"}


@pytest.mark.parametrize("kind", ["chain", "fleet", "outcomes"])
def test_cli_writes_a_file(tmp_path, kind):
    from agent_saga.cli import main

    wal = tmp_path / "a.wal"
    wal.write_text("\n".join(json.dumps(r) for r in _chained()), encoding="utf-8")
    out = tmp_path / f"{kind}.svg"
    assert main(["viz", "--wal", str(wal), "--kind", kind, "-o", str(out)]) == 0
    ET.fromstring(out.read_text(encoding="utf-8"))


def test_cli_surfaces_a_broken_chain(tmp_path):
    from agent_saga.cli import main

    records = _chained()
    records[3]["_ph"] = "e" * 64
    wal = tmp_path / "bad.wal"
    wal.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    out = tmp_path / "bad.svg"
    assert main(["viz", "--wal", str(wal), "--kind", "chain", "-o", str(out)]) == 0
    assert "CHAIN BROKEN" in out.read_text(encoding="utf-8")


def test_cli_reports_a_missing_wal_rather_than_traceback(tmp_path):
    from agent_saga.cli import main

    assert main(["viz", "--wal", str(tmp_path / "nope.wal")]) == 2
