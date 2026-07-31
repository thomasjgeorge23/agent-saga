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


def test_canvas_animation_is_disabled_under_reduced_motion(html):
    assert "REDUCED.matches" in html
    assert re.search(r"if \(REDUCED\.matches\) \{ canvas\.style\.display = 'none'", html)


def test_generated_svgs_carry_their_own_reduced_motion_block():
    for path in ASSETS.glob("*.svg"):
        svg = path.read_text(encoding="utf-8")
        assert "prefers-reduced-motion: reduce" in svg, path.name


# -- the build script is runnable and idempotent -----------------------------

def test_build_script_is_idempotent(tmp_path):
    """Running it twice must not change the tree the second time -- otherwise
    it cannot be used as a CI drift check."""
    before = {p.name: p.read_bytes() for p in ASSETS.glob("*.svg")}
    index_before = INDEX.read_bytes()

    proc = subprocess.run([sys.executable, str(SITE / "build_assets.py")],
                          capture_output=True, text=True, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr[-3000:]

    after = {p.name: p.read_bytes() for p in ASSETS.glob("*.svg")}
    assert after == before, "build_assets.py is not deterministic"
    assert INDEX.read_bytes() == index_before, "build_assets.py rewrote index.html"
