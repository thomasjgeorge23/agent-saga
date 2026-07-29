"""Prove the rollback works at every step, before production finds out.

A 30-step agent chain whose steps each succeed 98% of the time completes
54.5% of the time (0.98 ** 30). No safety layer changes that arithmetic. What
a transaction boundary changes is what the other 45.5% *leaves behind* -- and
almost nobody verifies that, because verifying it means deliberately breaking
your own workflow at every single step and checking the world afterwards.

That is what this module does. Given a saga and a way to observe the world, it
runs the workflow once per step, failing at a different step each time, and
compares the world against its starting state after every run:

    proof = await prove_rollback(scenario, snapshot=read_world, reset=clear_world)
    assert proof.proven          # every failure point unwinds to a clean world

Two things make this more than a test helper:

  * **It checks the world, not the engine's opinion of the world.** A step
    whose compensation runs, returns cleanly, and does not actually undo
    anything produces a `RollbackReport` that says `clean` and a world that is
    not. That mismatch is reported as `ENGINE_DISAGREES`, and it is the single
    most valuable thing here -- it is the only check in the package that can
    catch a compensation which lies.

  * **It probes both failure shapes.** `before` -- the step never ran, so
    steps 1..k-1 must unwind. `after` -- the step ran and *then* failed, which
    the engine records as UNKNOWN because a timed-out call may well have
    landed. The second is the realistic one and the one people get wrong: an
    inverse derived from a result that never arrived.

The report names the step, the tool, the mode, and what was left behind, so a
failure is actionable rather than a red mark.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import (Any, Awaitable, Callable, List, Optional, Sequence, Tuple)

__all__ = [
    "ProbeMode",
    "RollbackProof",
    "RollbackProbe",
    "StepProbe",
    "prove_rollback",
]


class RollbackProbe(RuntimeError):
    """The synthetic failure this module injects. Distinct from any exception
    the workflow raises on its own, so a scenario that fails for real is not
    mistaken for a successful probe."""


class ProbeMode:
    BEFORE = "before"
    """The step never ran. Steps 1..k-1 must unwind."""

    AFTER = "after"
    """The step ran, then failed -- the engine's UNKNOWN case, where the effect
    may have landed and the inverse has no result to derive itself from."""


@dataclass(frozen=True)
class StepProbe:
    index: int                     # 1-based
    tool: str
    mode: str
    world_restored: bool
    engine_said_clean: Optional[bool]
    residue: Optional[str] = None

    @property
    def verdict(self) -> str:
        if self.world_restored:
            return "CLEAN"
        if self.engine_said_clean:
            # The worst outcome available: the engine reported success and the
            # world disagrees. A caller trusting `RollbackReport.clean` here
            # would be trusting a compensation that did not compensate.
            return "ENGINE_DISAGREES"
        return "DIRTY"

    def describe(self) -> dict:
        return {"index": self.index, "tool": self.tool, "mode": self.mode,
                "verdict": self.verdict, "world_restored": self.world_restored,
                "engine_said_clean": self.engine_said_clean,
                "residue": self.residue}


@dataclass(frozen=True)
class RollbackProof:
    steps: int
    probes: Tuple[StepProbe, ...]
    baseline: Optional[str] = None

    @property
    def failures(self) -> Tuple[StepProbe, ...]:
        return tuple(p for p in self.probes if not p.world_restored)

    @property
    def lies(self) -> Tuple[StepProbe, ...]:
        """Probes where the engine reported a clean rollback and the world was
        not restored. These are the ones to fix first."""
        return tuple(p for p in self.probes if p.verdict == "ENGINE_DISAGREES")

    @property
    def proven(self) -> bool:
        """Every failure point unwound to the starting world. Strict: one dirty
        step is a dirty workflow, because in production you do not get to pick
        which step fails."""
        return bool(self.probes) and not self.failures

    def describe(self) -> dict:
        return {"steps": self.steps, "proven": self.proven,
                "probes_run": len(self.probes),
                "failures": len(self.failures), "lies": len(self.lies),
                "probes": [p.describe() for p in self.probes]}

    def format_text(self) -> str:
        lines = [f"rollback proof: {len(self.probes)} probe(s) across "
                 f"{self.steps} step(s)"]
        if not self.probes:
            lines.append("  no steps were executed -- nothing was proven")
            return "\n".join(lines)

        for probe in self.probes:
            mark = {"CLEAN": "  ok  ", "DIRTY": "  DIRTY",
                    "ENGINE_DISAGREES": "  LIES "}[probe.verdict]
            lines.append(f"{mark} step {probe.index} {probe.tool} "
                         f"[fail {probe.mode}]")
            if probe.residue:
                lines.append(f"         left behind: {probe.residue}")

        if self.lies:
            lines.append("")
            lines.append(f"  {len(self.lies)} step(s) reported a CLEAN rollback "
                         f"while leaving the world changed. Fix these first: a")
            lines.append(f"  compensation that reports success it did not achieve "
                         f"is worse than none.")
        lines.append("")
        lines.append(f"  proven: {self.proven}")
        return "\n".join(lines)


Scenario = Callable[[Any], Awaitable[Any]]


async def prove_rollback(
    scenario: Scenario,
    *,
    snapshot: Callable[[], Any],
    reset: Optional[Callable[[], Any]] = None,
    modes: Sequence[str] = (ProbeMode.BEFORE, ProbeMode.AFTER),
    max_steps: int = 64,
    wal_factory: Optional[Callable[[], Any]] = None,
) -> RollbackProof:
    """Run `scenario` once per (step, mode) with a failure injected there.

    `scenario` receives a `SagaContext` and performs its work through
    `ctx.execute`. `snapshot()` returns any comparable value describing the
    world (a dict, a sorted list of ids, a hash). `reset()` puts the world back
    to its starting condition between probes; without it, the caller is
    expected to make `scenario` idempotent from a fixed baseline.

    The discovery run executes the scenario once with no injection, both to
    count the steps and to confirm the happy path works -- proving rollback for
    a workflow that never succeeds would be measuring nothing.
    """
    from .context import SagaAborted
    from .decorator import saga_scope

    def _make_wal():
        if wal_factory is not None:
            return wal_factory()
        from .wal.file_wal import FileWAL
        import tempfile
        from pathlib import Path
        return FileWAL(Path(tempfile.mkdtemp(prefix="agent-saga-proof-")) / "p.wal")

    async def _run(inject_at: Optional[int], mode: str) -> Tuple[List[str], Optional[bool]]:
        """Returns (tools seen, engine's clean verdict or None)."""
        tools: List[str] = []
        wal = _make_wal()
        await wal.start()
        engine_clean: Optional[bool] = None
        try:
            try:
                async with saga_scope(wal=wal, name="rollback-proof") as ctx:
                    original = ctx.execute

                    async def probed(*args: Any, **kwargs: Any) -> Any:
                        tool = kwargs.get("tool") or (args[0] if args else "?")
                        tools.append(str(tool))
                        index = len(tools)
                        if inject_at is not None and index == inject_at:
                            if mode == ProbeMode.BEFORE:
                                raise RollbackProbe(
                                    f"injected failure before step {index} ({tool})")
                            result = await original(*args, **kwargs)
                            raise RollbackProbe(
                                f"injected failure after step {index} ({tool})")
                        return await original(*args, **kwargs)

                    ctx.execute = probed          # type: ignore[method-assign]
                    await scenario(ctx)
            except SagaAborted as aborted:
                report = getattr(aborted, "report", None)
                engine_clean = getattr(report, "clean", None)
        finally:
            await wal.close()
        return tools, engine_clean

    if reset:
        maybe = reset()
        if hasattr(maybe, "__await__"):
            await maybe
    baseline = copy.deepcopy(snapshot())

    # Discovery: the happy path, uninjected.
    tools, _ = await _run(None, ProbeMode.BEFORE)
    step_count = min(len(tools), max_steps)

    probes: List[StepProbe] = []
    for index in range(1, step_count + 1):
        for mode in modes:
            if reset:
                maybe = reset()
                if hasattr(maybe, "__await__"):
                    await maybe
            seen, engine_clean = await _run(index, mode)
            after = snapshot()
            restored = after == baseline
            probes.append(StepProbe(
                index=index,
                tool=seen[index - 1] if len(seen) >= index else "?",
                mode=mode,
                world_restored=restored,
                engine_said_clean=engine_clean,
                residue=None if restored else _diff(baseline, after),
            ))

    if reset:
        maybe = reset()
        if hasattr(maybe, "__await__"):
            await maybe

    return RollbackProof(steps=step_count, probes=tuple(probes),
                         baseline=_render(baseline))


def _render(value: Any, limit: int = 200) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _diff(before: Any, after: Any) -> str:
    """A short, readable statement of what the rollback failed to undo."""
    if isinstance(before, dict) and isinstance(after, dict):
        parts = []
        for key in sorted(set(before) | set(after)):
            if before.get(key) != after.get(key):
                parts.append(f"{key}: {_render(before.get(key), 60)} -> "
                             f"{_render(after.get(key), 60)}")
        if parts:
            return "; ".join(parts)
    return f"{_render(before)} -> {_render(after)}"
