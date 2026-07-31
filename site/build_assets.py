"""Regenerate the site's rollback animations from real engine runs.

    python site/build_assets.py

This is the script that makes the marketing page honest. The two figures in
the "Watch it happen" section are not drawn by hand and are not mock-ups: this
script executes three sagas through the actual `SagaContext`, lets them fail
for real, writes the resulting write-ahead logs, and renders them with
`agent_saga.animate`. The SVG is then spliced into `index.html` between
`<!-- BEGIN:name -->` / `<!-- END:name -->` markers.

The consequence worth stating: **the site cannot claim a rollback the engine
did not perform.** If someone breaks compensation ordering, or makes `clean`
too generous, this script's output changes and the picture on the home page
changes with it. `tests/test_site_assets.py` asserts the committed SVGs still
match a fresh run, so the drift is caught in CI rather than discovered by a
visitor.

The three scenarios are chosen to be the *honest* set, not the flattering one:

  clean   -- every step has a working inverse; the unwind is total.
  orphan  -- two steps declare COMPENSABLE and ship no inverse, so their
             effects survive the rollback. This one is on the page on purpose.
  success -- nothing fails; there is nothing to undo.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_saga import ActionSemantics, Compensation, SagaContext  # noqa: E402
from agent_saga.animate import wal_to_animated_svg                  # noqa: E402
from agent_saga.viz import (chain_ribbon, fleet_timeline,           # noqa: E402
                            outcome_matrix)
from agent_saga.wal import FileWAL                                  # noqa: E402

ASSETS = ROOT / "site" / "assets"
INDEX = ROOT / "site" / "index.html"


def _ok(**_kwargs):
    return {"id": "x"}


def _declined(**_kwargs):
    raise RuntimeError("payment processor declined")


def _undo(_result):
    return Compensation(lambda: None)


async def _run(path: Path, mode: str) -> bool:
    """Execute one scenario. Returns the engine's own `clean` verdict."""
    wal = FileWAL(str(path))
    await wal.start()
    # The saga id is normally random per run, which would make every rebuild
    # produce different bytes and defeat both `git diff` and the CI drift check
    # in tests/test_site_assets.py. Pinning it changes nothing about the
    # execution -- the steps, failures and rollback below are all real.
    saga = SagaContext(wal=wal, name="checkout", saga_id=f"checkout-{mode}-demo")
    await saga.begin()
    clean = True
    try:
        await saga.execute("stripe.charge", ActionSemantics.COMPENSABLE,
                           _ok, {"amount": 4200}, _undo)
        await saga.execute("inventory.reserve", ActionSemantics.COMPENSABLE,
                           _ok, {"sku": "A1"}, _undo)
        await saga.execute("ledger.post_entry", ActionSemantics.COMPENSABLE,
                           _ok, {"amount": 4200},
                           _undo if mode != "orphan" else None)
        if mode == "success":
            await saga.execute("crm.update", ActionSemantics.COMPENSABLE,
                               _ok, {"id": 9}, _undo)
        else:
            # The failing step is COMPENSABLE and its outcome is UNKNOWN -- the
            # call may still have landed -- so it needs an idempotent inverse
            # like every other step. Only `orphan` withholds one, deliberately.
            await saga.execute("crm.update", ActionSemantics.COMPENSABLE,
                               _declined, {"id": 9},
                               _undo if mode == "clean" else None)
        await saga.finish()
    except Exception as exc:                      # the scenario's own failure
        saga.record_abort(exc)
        report = await saga.rollback()
        clean = report.clean
        await saga.finish(aborted=True, clean=clean)
    await wal.close()
    return clean


def _splice(html: str, name: str, svg: str) -> str:
    """Replace the content between the named markers. Raises if a marker is
    missing -- silently writing nothing would leave a blank figure on the page
    and no error anywhere."""
    begin, end = f"<!-- BEGIN:{name} -->", f"<!-- END:{name} -->"
    pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end), re.DOTALL)
    if not pattern.search(html):
        raise SystemExit(f"marker pair for {name!r} not found in {INDEX}")
    return pattern.sub(lambda _m: f"{begin}\n{svg}\n          {end}", html, count=1)


SCENARIOS = (
    # (mode, asset filename, marker name, title, expected `clean` verdict)
    ("clean", "rollback-clean.svg", "rollback-clean",
     "Step 4 fails - every effect undone", True),
    ("orphan", "rollback-orphan.svg", "rollback-orphan",
     "Step 4 fails - two effects had no undo", False),
    ("success", "saga-success.svg", None,
     "All four steps commit", True),
)


async def _fleet(path: Path, sagas: int = 9) -> None:
    """A concurrent workload, so the timeline has real overlap to draw.

    Every third saga fails, and its rollback is a real rollback -- the bars the
    page shows in red were produced by the engine aborting, not by a colour
    chosen for effect.
    """
    wal = FileWAL(str(path))
    await wal.start()

    async def one(index: int) -> None:
        saga = SagaContext(wal=wal, name=f"order-{index:02d}",
                           saga_id=f"order-{index:02d}-demo")
        await saga.begin()
        try:
            await saga.execute("stripe.charge", ActionSemantics.COMPENSABLE,
                               _ok, {"amount": 1000 + index}, _undo)
            await saga.execute("inventory.reserve", ActionSemantics.COMPENSABLE,
                               _ok, {"sku": f"S{index}"}, _undo)
            forward = _declined if index % 3 == 2 else _ok
            await saga.execute("crm.update", ActionSemantics.COMPENSABLE,
                               forward, {"id": index}, _undo)
            await saga.finish()
        except Exception as exc:
            saga.record_abort(exc)
            report = await saga.rollback()
            await saga.finish(aborted=True, clean=report.clean)

    await asyncio.gather(*(one(i) for i in range(sagas)))
    await wal.close()


# Whole-log visuals, rendered from the fleet run above.
VIZ = (
    # (asset filename, marker name, renderer name, width)
    ("viz-fleet.svg", "viz-fleet", "fleet", 880),
    ("viz-chain.svg", "viz-chain", "chain", 880),
    ("viz-outcomes.svg", "viz-outcomes", "outcomes", 880),
)


def main() -> int:
    # The engine warns loudly about missing compensations. In the `orphan`
    # scenario that warning is the scenario, so quiet it rather than let it
    # read as a build failure.
    logging.getLogger("agent_saga").setLevel(logging.CRITICAL)

    ASSETS.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="saga-site-"))
    html = INDEX.read_text(encoding="utf-8")

    for mode, filename, marker, title, expected_clean in SCENARIOS:
        wal_path = tmp / f"{mode}.wal"
        clean = asyncio.run(_run(wal_path, mode))
        if clean != expected_clean:
            # The engine disagreed with what this page is about to claim. Stop.
            raise SystemExit(
                f"scenario {mode!r} expected clean={expected_clean} but the "
                f"engine reported clean={clean}. The site would have shown a "
                f"verdict the engine did not reach -- fix the scenario or the "
                f"engine, do not publish this.")

        records = [json.loads(line) for line
                   in wal_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        svg = wal_to_animated_svg(records, title=title, width=880)

        (ASSETS / filename).write_text(svg + "\n", encoding="utf-8")
        if marker:
            html = _splice(html, marker, svg)
        print(f"  {filename:22} {len(svg):6} bytes  engine clean={clean}")

    # -- whole-log visuals, from one concurrent run --------------------------
    fleet_wal = tmp / "fleet.wal"
    asyncio.run(_fleet(fleet_wal))
    fleet_records = [json.loads(line) for line
                     in fleet_wal.read_text(encoding="utf-8").splitlines()
                     if line.strip()]

    renderers = {"fleet": fleet_timeline, "chain": chain_ribbon,
                 "outcomes": outcome_matrix}
    for filename, marker, kind, width in VIZ:
        svg = renderers[kind](fleet_records, width=width)
        if kind == "chain" and "CHAIN BROKEN" in svg:
            # The generator just produced this log; a broken chain here means
            # the WAL writer is wrong, not that the page has an interesting
            # picture to show.
            raise SystemExit(
                "the freshly written fleet WAL reports a broken hash chain. "
                "That is a WAL bug, not a rendering one -- do not publish.")
        (ASSETS / filename).write_text(svg + "\n", encoding="utf-8")
        html = _splice(html, marker, svg)
        print(f"  {filename:22} {len(svg):6} bytes  ({kind})")

    INDEX.write_text(html, encoding="utf-8")
    spliced = sum(1 for s in SCENARIOS if s[2]) + len(VIZ)
    print(f"spliced {spliced} visual(s) into {INDEX.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
