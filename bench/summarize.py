"""Aggregate benchmark runs into the numbers we are willing to publish.

Reports median-of-runs per percentile. One sample of a tail is not a
measurement -- if the p99 spread across identical runs is wide, that is itself
the finding, and this script says so rather than picking the flattering run.
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path


def main(root: str) -> None:
    runs: dict[str, list[dict]] = defaultdict(list)
    envs: list[dict] = []

    for path in sorted(Path(root).rglob("results-*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        envs.append(data["environment"])
        gc_state = "gc-off" if not data["environment"].get("gc_enabled", True) else "gc-on"
        for profile in data["profiles"]:
            runs[f"{profile['profile']} [{gc_state}]"].append(profile)

    if not runs:
        print("no benchmark results found")
        return

    env = envs[0]
    print("## benchmark summary\n")
    print(f"- python {env['python']} on {env['os']} {env['release']} ({env['machine']})")
    print(f"- timer resolution {env.get('timer_resolution_ns', 0):.1f} ns")
    print(f"- {len(envs)} runs aggregated\n")

    print("| profile | runs | p50 ms | p95 ms | p99 ms (median) | p99 spread | max ms |")
    print("|---|---|---|---|---|---|---|")
    for name, samples in sorted(runs.items()):
        p99s = [s["p99_ms"] for s in samples]
        spread = max(p99s) - min(p99s)
        flag = " ⚠️" if p99s and spread > statistics.median(p99s) * 0.5 else ""
        print(f"| {name} | {len(samples)} | "
              f"{statistics.median(s['p50_ms'] for s in samples):.4f} | "
              f"{statistics.median(s['p95_ms'] for s in samples):.4f} | "
              f"{statistics.median(p99s):.4f} | "
              f"{spread:.4f}{flag} | "
              f"{max(s['max_ms'] for s in samples):.3f} |")

    print("\n⚠️ = p99 varied by more than 50% across identical runs; treat as "
          "unstable and do not publish that figure.\n")

    gc_on = [s for k, v in runs.items() if "gc-on" in k and "FAST" in k for s in v]
    gc_off = [s for k, v in runs.items() if "gc-off" in k and "FAST" in k for s in v]
    if gc_on and gc_off:
        on_max = max(s["max_ms"] for s in gc_on)
        off_max = max(s["max_ms"] for s in gc_off)
        print(f"**Fast-path tail, GC isolation:** max {on_max:.3f} ms with GC, "
              f"{off_max:.3f} ms without.")
        if on_max > off_max * 3:
            print("The outlier is a CPython collection pause, not library overhead.")
        else:
            print("GC does not explain the outlier -- look elsewhere "
                  "(scheduler, page faults, thread pool wakeup).")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "artifacts")
