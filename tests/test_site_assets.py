"""The marketing page is not allowed to drift away from the engine.

`site/build_assets.py` renders the home page's rollback animations from real
runs of the real `SagaContext`. These tests assert that what is committed in
`site/` still matches what the engine does today, and that the numbers printed
beside them are the numbers this repository can actually produce.

The failure mode being defended against is specific and unglamorous: someone
changes compensation ordering or loosens `RollbackReport.clean`, every unit
test still passes because they were updated too, and the home page keeps
showing an animation of behaviour that no longer exists. A screenshot is
evidence right up until it is stale.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
INDEX = SITE / "index.html"
ASSETS = SITE / "assets"

pytestmark = pytest.mark.skipif(not INDEX.exists(), reason="site/ not present")


@pytest.fixture(scope="module")
def html() -> str:
    return INDEX.read_text(encoding="utf-8")


# -- the committed animations still match a fresh engine run -----------------

def _regenerate(tmp_path: Path) -> dict:
    """Run the three scenarios in a subprocess and return `{name: svg}`.

    A subprocess because `build_assets.py` registers compensations and mutates
    logging levels; importing it here would leak both into the rest of the run.
    """
    script = tmp_path / "gen.py"
    script.write_text(
        "import asyncio, json, logging, sys, tempfile\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        f"sys.path.insert(0, {str(SITE)!r})\n"
        "logging.getLogger('agent_saga').setLevel(logging.CRITICAL)\n"
        "import build_assets as b\n"
        "from agent_saga.animate import wal_to_animated_svg\n"
        "from pathlib import Path\n"
        "out = {}\n"
        "tmp = Path(tempfile.mkdtemp())\n"
        "for mode, filename, marker, title, expected in b.SCENARIOS:\n"
        "    p = tmp / (mode + '.wal')\n"
        "    clean = asyncio.run(b._run(p, mode))\n"
        "    recs = [json.loads(l) for l in p.read_text('utf-8').splitlines() if l.strip()]\n"
        "    out[filename] = {'svg': wal_to_animated_svg(recs, title=title, width=880),\n"
        "                     'clean': clean, 'expected': expected}\n"
        "print(json.dumps(out))\n",
        encoding="utf-8")
    proc = subprocess.run([sys.executable, str(script)], capture_output=True,
                          text=True, cwd=str(ROOT),
                          env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def fresh(tmp_path_factory) -> dict:
    return _regenerate(tmp_path_factory.mktemp("siteassets"))


def test_engine_still_reaches_the_verdicts_the_page_shows(fresh):
    """The scenario contract, checked against the engine rather than a fixture.

    `clean` on the left-hand figure and `not clean` on the right-hand one are
    the entire claim the section makes.
    """
    for filename, got in fresh.items():
        assert got["clean"] == got["expected"], (
            f"{filename}: page is built for clean={got['expected']} but the "
            f"engine now reports clean={got['clean']}")


@pytest.mark.parametrize("filename", ["rollback-clean.svg", "rollback-orphan.svg",
                                      "saga-success.svg"])
def test_committed_svg_matches_a_fresh_run(fresh, filename):
    committed = (ASSETS / filename)
    assert committed.exists(), (
        f"{filename} is missing. Run `python site/build_assets.py`.")
    assert committed.read_text(encoding="utf-8").strip() == fresh[filename]["svg"].strip(), (
        f"{filename} is stale -- the engine's behaviour changed since it was "
        f"rendered. Run `python site/build_assets.py` and commit the result.")


def test_inlined_svg_matches_the_asset_file(html):
    """The page inlines the SVG; the file on disk is the same bytes."""
    for name in ("rollback-clean", "rollback-orphan"):
        match = re.search(re.escape(f"<!-- BEGIN:{name} -->") + r"(.*?)"
                          + re.escape(f"<!-- END:{name} -->"), html, re.DOTALL)
        assert match, f"marker pair for {name} missing from index.html"
        inlined = match.group(1).strip()
        assert inlined.startswith("<svg"), f"{name} marker block is empty"
        on_disk = (ASSETS / f"{name}.svg").read_text(encoding="utf-8").strip()
        assert inlined == on_disk, (
            f"{name}: index.html and assets/{name}.svg disagree. "
            f"Run `python site/build_assets.py`.")


def test_the_page_shows_the_unflattering_case(html):
    """An incomplete rollback is on the home page on purpose. If this test ever
    fails because someone removed it, that is a positioning decision that
    should be argued for, not made quietly."""
    assert "ROLLBACK INCOMPLETE" in html
    assert "ORPHANED" in html


def test_inlined_animations_are_well_formed(html):
    for name in ("rollback-clean", "rollback-orphan"):
        match = re.search(re.escape(f"<!-- BEGIN:{name} -->") + r"(.*?)"
                          + re.escape(f"<!-- END:{name} -->"), html, re.DOTALL)
        ET.fromstring(match.group(1).strip())


# -- the numbers beside them -------------------------------------------------

def _stat(html: str, suffix: str, label_fragment: str) -> float:
    """Pull one `data-count` whose <span> mentions `label_fragment`."""
    for match in re.finditer(
            r'<b data-count="([\d.]+)"[^>]*>.*?</b>\s*<span>(.*?)</span>',
            html, re.DOTALL):
        if label_fragment in match.group(2):
            return float(match.group(1))
    raise AssertionError(f"no stat matching {label_fragment!r} on the page")


def test_adapter_count_on_the_page_matches_the_package(html):
    modules = {p.stem for p in (ROOT / "agent_saga" / "adapters").glob("*.py")
               if p.stem not in ("__init__", "_common")}
    assert _stat(html, "", "framework adapters") == len(modules), (
        f"page says N adapters; package has {len(modules)}: {sorted(modules)}")


def test_marquee_lists_exactly_the_adapters_that_exist(html):
    match = re.search(r"var adapters = \[(.*?)\];", html, re.DOTALL)
    assert match, "adapter list not found in index.html"
    listed = set(re.findall(r"'([a-z_]+)'", match.group(1)))
    modules = {p.stem for p in (ROOT / "agent_saga" / "adapters").glob("*.py")
               if p.stem not in ("__init__", "_common")}
    assert listed == modules, (
        f"marquee and package disagree. only-on-page={sorted(listed - modules)} "
        f"only-in-package={sorted(modules - listed)}")


def test_interleaving_count_matches_the_verifier(html):
    """The number on the page is not a slogan; it is what
    `verify_rollback_invariants(max_steps=6)` actually enumerates. So this test
    runs it, rather than grepping the source for a literal that could be a
    coincidence."""
    import asyncio

    from agent_saga.verification import verify_rollback_invariants

    claimed = int(_stat(html, "", "failure interleavings"))
    report = asyncio.run(verify_rollback_invariants(max_steps=6))
    actual = report.interleavings
    assert claimed == actual, (
        f"the page claims {claimed} interleavings; the verifier enumerates "
        f"{actual}")
    assert not report.violations, (
        f"the page claims these are proven, but the verifier found "
        f"{len(report.violations)} violation(s)")


def test_test_count_on_the_page_is_not_inflated(html):
    """'N tests passing' is a claim about this repository, so check it against
    this repository. Collection is used rather than a full run: it is fast, and
    it cannot be gamed by a test that passes vacuously."""
    claimed = int(_stat(html, "", "tests passing"))
    proc = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q"],
                          capture_output=True, text=True, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stdout[-2000:]
    match = re.search(r"(\d+) tests collected", proc.stdout)
    assert match, proc.stdout[-2000:]
    collected = int(match.group(1))

    # The page counts *passing* tests; collection counts everything, including
    # the handful that skip on this platform. A small gap is expected; the page
    # claiming more than exist is not.
    assert claimed <= collected, (
        f"the page claims {claimed} tests pass but only {collected} exist")
    assert collected - claimed <= 25, (
        f"the page claims {claimed} but {collected} are collected -- the number "
        f"is stale. Run `pytest -q` and update it.")


def test_no_stale_version_string_on_the_page(html):
    """The footer said v0.4.2 while the nav badge said v0.5.4, for three
    releases. A version number is the cheapest possible signal that a page is
    maintained, so it is worth a test."""
    from agent_saga._version import __version__

    # Only versions that are unambiguously agent-saga's: ones written straight
    # after the package name. Tags are stripped first so the nav badge
    # (`agent-saga <span>v0.5.4</span>`) is caught too. A bare `2.0.0` on the
    # page is a SQLAlchemy pin, `127.0.0.1` is an address, and `v2.0.0` is
    # sample text in a demo input -- none of them are ours to keep current.
    text = re.sub(r"<[^>]+>", " ", html)
    found = set(re.findall(r"agent-saga\s+v?(\d+\.\d+\.\d+)", text))
    found |= set(re.findall(r"PyPI\s+v?(\d+\.\d+\.\d+)", text))
    assert found, "no agent-saga version found on the page at all"
    stale = {v for v in found if v != __version__}
    assert not stale, (
        f"page shows agent-saga version(s) {sorted(stale)} but this is "
        f"{__version__}")


def test_generated_visual_markers_are_all_filled(html):
    """Every BEGIN/END pair must contain a real SVG. An empty marker block is a
    blank figure on a live page with nothing in the console to say why."""
    pairs = re.findall(r"<!-- BEGIN:([\w-]+) -->(.*?)<!-- END:\1 -->", html, re.DOTALL)
    assert len(pairs) >= 5, f"expected at least 5 generated visuals, found {len(pairs)}"
    for name, body in pairs:
        assert body.strip().startswith("<svg"), f"{name} marker block is empty"
        ET.fromstring(body.strip())


def test_page_views_are_siblings_not_nested(html):
    """Two of the five tabs rendered completely blank.

    `<div id="page-sdk">` was never closed before `</main>`, and a `<section>`
    inside page-industries was missing both its `</div>` and `</section>`. The
    HTML parser's error recovery reparented page-sandbox, page-sdk and
    page-inquiry *inside* page-industries -- so `display:none` on the ancestor
    beat `active-view` on the child, and selecting Sandbox or SDK & Docs showed
    nothing at all. Balanced tags are the whole defence, so they get a test.
    """
    views = re.findall(r'<div id="(page-[\w-]+)" class="page-view', html)
    ends = re.findall(r"</div> <!-- END (PAGE-[\w-]+) -->", html)
    assert len(views) == len(ends), (
        f"{len(views)} page-view opens but {len(ends)} END markers")

    for view in views:
        start = html.index(f'<div id="{view}" class="page-view')
        end_marker = f"</div> <!-- END {view.upper()} -->"
        assert end_marker in html, f"{view} has no END marker"
        block = html[start:html.index(end_marker, start) + len(end_marker)]

        divs = len(re.findall(r"<div\b", block)) - len(re.findall(r"</div>", block))
        secs = len(re.findall(r"<section\b", block)) - len(re.findall(r"</section>", block))
        assert divs == 0, f"{view} leaves {divs} <div> unclosed"
        assert secs == 0, f"{view} leaves {secs} <section> unclosed"

        # No other view may start inside this one.
        inner = re.findall(r'<div id="(page-[\w-]+)" class="page-view', block)
        assert inner == [view], f"{view} contains {[v for v in inner if v != view]}"

    assert html.count("</main>") == 1, "more than one </main>"


def test_every_tab_has_a_generated_visual(html):
    """The four non-overview tabs were walls of text. Each should now carry at
    least one figure rendered from real execution data."""
    for view in ("page-overview", "page-industries", "page-sandbox", "page-sdk"):
        start = html.index(f'<div id="{view}" class="page-view')
        nxt = html.find('<div id="page-', start + 10)
        section = html[start:nxt if nxt != -1 else len(html)]
        assert "<!-- BEGIN:" in section, f"{view} has no generated visual"


def test_dependency_count_claim_is_true():
    """'0 required dependencies' has to survive contact with pyproject.toml."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r"^dependencies = \[(.*?)\]", text, re.MULTILINE | re.DOTALL)
    assert match, "no [project] dependencies key"
    assert not match.group(1).strip(), (
        f"the page claims zero required dependencies but pyproject declares: "
        f"{match.group(1).strip()}")


# -- motion accessibility ----------------------------------------------------

def test_reveal_hidden_state_is_scoped_to_no_preference(html):
    """The load-bearing accessibility property of the motion layer.

    `[data-reveal] { opacity: 0 }` must live inside
    `@media (prefers-reduced-motion: no-preference)`. If it escapes that block,
    a visitor with reduced motion enabled -- or anyone whose JS fails to run --
    gets a page of invisible content.
    """
    match = re.search(r"@media \(prefers-reduced-motion: no-preference\) \{(.*?)\n  \}",
                      html, re.DOTALL)
    assert match, "no-preference block not found"
    inside = match.group(1)
    assert "[data-reveal] {" in inside
    assert "opacity: 0" in inside

    # And nowhere outside it.
    outside = html.replace(match.group(0), "")
    assert not re.search(r"\[data-reveal\]\s*\{[^}]*opacity:\s*0", outside), (
        "a [data-reveal] rule sets opacity:0 outside the no-preference block")


def test_javascript_failure_cannot_blank_the_page(html):
    """Same property from the other direction: nothing in the stylesheet may
    hide content behind a class that only JS can add."""
    assert ".is-in" in html          # the class exists
    # ...but the hidden state it reverses is inside the no-preference query,
    # which the previous test pins. Here we just check no global hider exists.
    assert not re.search(r"^\s*body\s*\{[^}]*opacity:\s*0", html, re.MULTILINE)


def test_word_splitter_leaves_gradient_clipped_text_alone(html):
    """`.hero .amber` paints through `-webkit-background-clip: text`. Splitting
    it into per-word spans made each span inherit
    `-webkit-text-fill-color: transparent` with no background of its own to
    clip -- the headline laid out perfectly and rendered as nothing. The
    splitter must detect that and reveal such elements whole."""
    assert "paintsViaBackground" in html
    assert "word-soft" in html
    # The detection has to cover both spellings; browsers disagree on which
    # computed property they report.
    assert "webkitBackgroundClip" in html and "webkitTextFillColor" in html
    # And the atomic variant must not carry a transform, which does nothing on
    # an inline box and would silently not animate.
    match = re.search(r"\.word-soft \{(.*?)\}", html, re.DOTALL)
    assert match, "no .word-soft rule"
    assert "transform" not in match.group(1)


def test_hero_shader_has_a_lifecycle(html):
    """A background shader that runs off-screen, in a hidden tab, or at 3x DPR
    is a battery bug wearing a nice gradient."""
    assert "IntersectionObserver" in html
    assert "visibilitychange" in html
    assert re.search(r"devicePixelRatio \|\| 1, 1\.5", html), "DPR is not capped"
    assert "fallbackParticles" in html, "no non-WebGL fallback"


def test_canvas_animation_is_disabled_under_reduced_motion(html):
    assert "REDUCED.matches" in html
    assert re.search(r"if \(REDUCED\.matches\) \{ canvas\.style\.display = 'none'", html)


def test_generated_svgs_carry_their_own_reduced_motion_block():
    for path in ASSETS.glob("*.svg"):
        svg = path.read_text(encoding="utf-8")
        assert "prefers-reduced-motion: reduce" in svg, path.name


# -- the build script is runnable and idempotent -----------------------------

# The single-saga animations run sequentially with pinned saga ids, so they are
# byte-reproducible. The whole-log visuals come from a genuinely concurrent run
# -- nine sagas racing through one WAL -- and a real concurrent run does not
# produce identical bytes twice: the interleaving differs, and so do the
# timestamps and therefore the hashes. Forcing those to be stable would mean
# faking the concurrency the fleet timeline exists to show, so they are pinned
# on meaning instead of bytes.
REPRODUCIBLE = ["rollback-clean.svg", "rollback-orphan.svg", "saga-success.svg"]
CONCURRENT = ["viz-fleet.svg", "viz-chain.svg", "viz-outcomes.svg"]


def test_build_script_is_idempotent_for_the_sequential_scenarios(tmp_path):
    """The three single-saga animations must be byte-identical across rebuilds,
    otherwise `git status` is noisy after every build and the drift check above
    is meaningless."""
    before = {name: (ASSETS / name).read_bytes() for name in REPRODUCIBLE}

    proc = subprocess.run([sys.executable, str(SITE / "build_assets.py")],
                          capture_output=True, text=True, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr[-3000:]

    after = {name: (ASSETS / name).read_bytes() for name in REPRODUCIBLE}
    assert after == before, "the sequential scenarios are not deterministic"


@pytest.mark.parametrize("name", CONCURRENT)
def test_concurrent_visuals_keep_their_meaning_across_rebuilds(name):
    """These bytes legitimately change every build. What must not change is
    what they say: an intact chain, a fleet with real aborts in it, and an
    outcome matrix that saw effects taken back."""
    svg = (ASSETS / name).read_text(encoding="utf-8")
    ET.fromstring(svg)

    if name == "viz-chain.svg":
        assert "chain intact" in svg
        assert "CHAIN BROKEN" not in svg and "PARTIALLY CHAINED" not in svg
    elif name == "viz-fleet.svg":
        assert "aborted and rolled back" in svg
        assert "no timestamped saga records" not in svg
    else:
        assert "effect(s) had to be taken back" in svg
