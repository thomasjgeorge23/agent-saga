"""Readiness audit and project scaffold: assembly that is correct on day one.

The claims under test:
1. The three postures that silently lose effects are each detected: a
   process-local WAL under multiple replicas, silent record dropping, and
   in-process-only compensations.
2. Severity is honest -- a risk becomes a blocker only when the deployment
   makes it arithmetic rather than hypothetical, and `production_ready` keys
   off blockers alone.
3. A clean report is evidence of checks that ran, not of checks that were
   skipped.
4. The scaffold generates a project that compiles, and whose own tests pass --
   including one proving the rollback runs and one proving every compensation
   is recoverable across processes.
5. The generator refuses to clobber edited files without --force.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from agent_saga.readiness import BLOCKER, NOTE, RISK, audit
from agent_saga.scaffold import render, write_project
from agent_saga.wal.base import BackpressurePolicy
from agent_saga.wal.file_wal import FileWAL


def codes(report):
    return {f.code: f.severity for f in report.findings}


# -- 1 & 2. the postures that lose effects, at honest severity ---------------------

def test_a_process_local_wal_is_a_risk_alone_and_a_blocker_in_a_fleet(tmp_path):
    wal = FileWAL(tmp_path / "w.wal")

    assert codes(audit(wal=wal))["wal-shared"] == RISK
    assert codes(audit(wal=wal, replicas=1))["wal-shared"] == RISK

    fleet = audit(wal=wal, replicas=3)
    assert codes(fleet)["wal-shared"] == BLOCKER
    assert not fleet.production_ready
    detail = next(f for f in fleet.findings if f.code == "wal-shared").detail
    assert "3 replicas" in detail          # says why it is not hypothetical


def test_silent_record_dropping_is_a_blocker(tmp_path):
    wal = FileWAL(tmp_path / "w.wal", backpressure=BackpressurePolicy.DROP_SILENT)
    report = audit(wal=wal)
    assert codes(report)["wal-backpressure"] == BLOCKER
    assert not report.production_ready


def test_records_already_dropped_is_a_blocker(tmp_path):
    wal = FileWAL(tmp_path / "w.wal")
    wal.dropped = 4
    report = audit(wal=wal)
    assert codes(report)["wal-dropped"] == BLOCKER
    assert "4 record(s)" in next(
        f for f in report.findings if f.code == "wal-dropped").summary


def test_an_unchained_log_and_an_unbounded_fence_are_risks(tmp_path):
    wal = FileWAL(tmp_path / "w.wal", chain=False, barrier_timeout=None)
    found = codes(audit(wal=wal))
    assert found["wal-chain"] == RISK
    assert found["wal-barrier-timeout"] == RISK


def test_a_healthy_wal_raises_none_of_the_wal_findings(tmp_path):
    """The checker must not cry wolf: a correctly configured file WAL on a
    single replica reports only the shared-log risk, which is true."""
    wal = FileWAL(tmp_path / "w.wal")
    found = codes(audit(wal=wal, replicas=1))
    for clean in ("wal-backpressure", "wal-dropped", "wal-chain",
                  "wal-barrier-timeout", "wal-configured"):
        assert clean not in found


def test_no_default_wal_is_reported_rather_than_assumed_fine():
    report = audit(wal=None)
    assert codes(report)["wal-configured"] == RISK


def test_compensation_recoverability_is_checked(tmp_path):
    """This package registers handlers at import time (codemod.restore_files,
    the connectors), so the finding is absent here -- but the check must have
    run, or a clean report would prove nothing."""
    report = audit(wal=FileWAL(tmp_path / "w.wal"))
    assert "compensations-recoverable" in report.checked


# -- 3. a clean report is evidence of work done ---------------------------------------

def test_every_check_is_named_in_the_report(tmp_path):
    report = audit(wal=FileWAL(tmp_path / "w.wal"), replicas=1)
    for expected in ("wal-configured", "wal-shared", "wal-backpressure",
                     "wal-dropped", "wal-chain", "wal-barrier-timeout",
                     "compensations-recoverable", "wal-encryption", "telemetry"):
        assert expected in report.checked
    assert report.describe()["checked"] == list(report.checked)


def test_findings_are_ordered_worst_first(tmp_path):
    wal = FileWAL(tmp_path / "w.wal", backpressure=BackpressurePolicy.DROP_SILENT,
                  chain=False)
    severities = [f.severity for f in audit(wal=wal, replicas=2).findings]
    ranks = {BLOCKER: 0, RISK: 1, NOTE: 2}
    assert severities == sorted(severities, key=lambda s: ranks[s])


def test_notes_alone_never_block_production(tmp_path):
    report = audit(wal=FileWAL(tmp_path / "w.wal"), replicas=1)
    assert report.production_ready          # only a risk + notes
    assert report.risks


# -- the doctor CLI ---------------------------------------------------------------------

def test_doctor_exits_nonzero_on_blockers_and_under_strict(tmp_path, capsys):
    from agent_saga.cli import main

    wal_file = tmp_path / "agent-saga.wal"
    wal_file.write_text('{"seq":1,"event":"SAGA_START","saga_id":"x"}\n',
                        encoding="utf-8")

    assert main(["doctor", "--wal", str(wal_file), "--replicas", "1"]) == 0
    assert main(["doctor", "--wal", str(wal_file), "--replicas", "4"]) == 1
    assert main(["doctor", "--wal", str(wal_file), "--strict"]) == 1
    assert "readiness" in capsys.readouterr().out


# -- 4 & 5. the scaffold ------------------------------------------------------------------

def test_render_produces_every_file_with_the_project_name_substituted():
    files = render("myagent", "0.4.2")
    assert set(files) >= {"README.md", "app/service.py", "app/tools.py",
                          "app/settings.py", "tests/test_rollback.py",
                          "Dockerfile", "docker-compose.yml", ".env.example",
                          "requirements.txt"}
    assert "myagent" in files["README.md"]
    assert "agent-saga[postgres,redis,otel]>=0.4.2" in files["requirements.txt"]
    # no unsubstituted template holes anywhere
    for path, content in files.items():
        assert "{name}" not in content and "{version}" not in content, path


@pytest.mark.parametrize("bad", ["", "has space", "semi;colon", "../escape"])
def test_render_refuses_an_unsafe_project_name(bad):
    with pytest.raises(ValueError):
        render(bad, "0.4.2")


def test_generated_project_compiles_and_its_own_tests_pass(tmp_path):
    """The scaffold's whole promise is 'correct on the first run'. The only
    way to keep that promise honest is to actually run what it wrote."""
    target = tmp_path / "myagent"
    written = write_project("myagent", target, "0.4.2")
    assert len(written) == 10

    import py_compile
    for path in target.rglob("*.py"):
        py_compile.compile(str(path), doraise=True)

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=target, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout


def test_the_generated_app_uses_a_registered_compensator(tmp_path):
    """The one thing a hand-rolled try/except cannot do. If this stops being
    true, the scaffold is teaching the wrong pattern."""
    tools = render("myagent", "0.4.2")["app/tools.py"]
    assert '@compensator("inventory.release")' in tools
    assert 'handler="inventory.release"' in tools
    assert "ActionSemantics.IRREVERSIBLE" in tools      # and declares the hard one


def test_write_project_refuses_to_clobber_without_force(tmp_path):
    target = tmp_path / "myagent"
    write_project("myagent", target, "0.4.2")
    (target / "app" / "tools.py").write_text("# my edits\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="--force"):
        write_project("myagent", target, "0.4.2")
    assert (target / "app" / "tools.py").read_text(encoding="utf-8") == "# my edits\n"

    write_project("myagent", target, "0.4.2", force=True)
    assert "compensator" in (target / "app" / "tools.py").read_text(encoding="utf-8")


def test_new_cli_creates_a_project(tmp_path, capsys):
    from agent_saga.cli import main

    assert main(["new", "acme", "-d", str(tmp_path / "acme")]) == 0
    assert (tmp_path / "acme" / "app" / "service.py").exists()
    assert "created 10 files" in capsys.readouterr().out

    assert main(["new", "acme", "-d", str(tmp_path / "acme")]) == 1   # no clobber
