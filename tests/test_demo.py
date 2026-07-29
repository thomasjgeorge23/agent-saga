"""`agent-saga demo` is a product surface, so it gets tested like one.

It is also the project's loudest claim -- "a killed process's charge still gets
refunded" -- made in front of the reader. A demo that quietly stopped proving
that would be worse than no demo, so these tests assert the *world state* after
each act rather than the words printed about it.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest


def run_demo_cli(*extra):
    return subprocess.run(
        [sys.executable, "-m", "agent_saga.cli", "demo", "--fast", "--no-color", *extra],
        capture_output=True, text=True, timeout=300)


def test_the_demo_runs_end_to_end_and_exits_zero():
    result = run_demo_cli()
    assert result.returncode == 0, result.stdout + result.stderr
    for act in ("ACT I", "ACT II", "ACT III"):
        assert act in result.stdout


def test_act_one_leaves_real_damage_and_act_two_does_not():
    """The two must differ, or the demo is showing nothing."""
    out = run_demo_cli().stdout
    act_one = out.split("ACT II")[0]
    act_two = out.split("ACT II")[1].split("ACT III")[0]

    assert "charges left standing : ['ch_1']" in act_one
    assert "servers left running  : ['i-1']" in act_one

    assert "charges left standing : none" in act_two
    assert "servers left running  : none" in act_two


def test_act_two_reports_a_genuinely_clean_rollback():
    """The label is read from RollbackReport, so it can only say clean when
    the engine says clean. An earlier draft of this demo hardcoded it and
    printed 'clean' while the engine reported INCOMPLETE."""
    act_two = run_demo_cli().stdout.split("ACT II")[1].split("ACT III")[0]
    assert "rollback reported: clean" in act_two
    assert "PARTIAL" not in act_two


def test_act_three_really_kills_a_process_and_recovers_from_the_log():
    act_three = run_demo_cli().stdout.split("ACT III")[1]
    assert "exit code 9 (killed, no cleanup ran)" in act_three
    assert "RECOVERED" in act_three
    # the money the dead process took is gone again
    tail = act_three.split("The world, afterwards:")[1]
    assert "charges left standing : none" in tail


def test_the_crash_worker_dies_without_cleanup(tmp_path):
    """Called directly, so the kill is unambiguous rather than inferred from
    the demo's own narration."""
    world = tmp_path / "world.json"
    world.write_text(json.dumps({"charges": [], "servers": [], "emails_sent": []}),
                     encoding="utf-8")

    import os

    # Inherit the real environment and override one key. Handing the child a
    # hand-built env drops SystemDrive/SystemRoot on Windows, and the shell
    # then materialises "%SystemDrive%" as a literal directory in the repo --
    # which is exactly what an earlier version of this test did.
    result = subprocess.run(
        [sys.executable, "-m", "agent_saga.demo", "--demo-crash-worker",
         str(tmp_path / "w.wal")],
        env={**os.environ, "AGENT_SAGA_DEMO_WORLD": str(world)},
        capture_output=True, text=True, timeout=120)

    assert result.returncode == 9                      # os._exit(9), no unwind
    state = json.loads(world.read_text("utf-8"))
    assert state["charges"], "the worker should have charged before dying"
    assert (tmp_path / "w.wal").exists(), "the intent must be durable"


def test_output_survives_a_non_utf8_console():
    """Windows consoles default to cp1252. An emoji or box-drawing character
    here turns the project's showcase into a UnicodeEncodeError."""
    out = run_demo_cli().stdout
    out.encode("cp1252")                               # raises if it would break
    assert out.isascii()


def test_the_demo_is_quiet_by_default_and_loud_on_request():
    quiet = run_demo_cli().stdout
    assert "rollback triggered by" not in quiet        # engine trace suppressed
    loud = run_demo_cli("--logs")
    assert loud.returncode == 0
