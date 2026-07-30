"""`agent-saga adopt`: do the mechanical half, refuse the judgement half.

The honest weakness this addresses is onboarding, not capability. What it must
never do in the name of onboarding is make the one decision the whole engine
rests on -- so the generated module emits `semantics=DECIDE`, and `DECIDE`
raises the moment anything touches it. The file does not run until a human has
been through it, which is deliberate friction in the one place friction pays.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from agent_saga.adopt import analyse

AGENT_PROJECT = {
    "app/__init__.py": "",
    "app/tools.py": (
        "from langchain_core.tools import tool, StructuredTool\n"
        "\n"
        "@tool\n"
        "def search_docs(query: str):\n"
        '    """Search the docs."""\n'
        "    return []\n"
        "\n"
        "@tool\n"
        "def send_welcome_email(to: str):\n"
        '    """Email a new user."""\n'
        "    return {'sent': to}\n"
        "\n"
        "def charge_customer(amount: int):\n"
        '    """Charge a card."""\n'
        "    return {'id': 'ch_1'}\n"
        "\n"
        "charge_tool = StructuredTool.from_function(charge_customer)\n"
    ),
    "app/crew.py": (
        "from crewai.tools import BaseTool\n"
        "\n"
        "class DeleteRecordTool(BaseTool):\n"
        "    name = 'delete_record'\n"
        "    def _run(self, record_id: str):\n"
        "        return {'deleted': record_id}\n"
    ),
}


def write_project(root: Path) -> Path:
    for rel, text in AGENT_PROJECT.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
    return root


# -- detection -------------------------------------------------------------------

def test_it_finds_tools_across_frameworks(tmp_path):
    plan = analyse(write_project(tmp_path))

    assert set(plan.frameworks) == {"langchain", "crewai"}
    names = {c.name for c in plan.candidates}
    assert names == {"search_docs", "send_welcome_email", "charge_customer",
                     "DeleteRecordTool"}
    kinds = {c.name: c.kind for c in plan.candidates}
    assert kinds["search_docs"] == "decorated"
    assert kinds["charge_customer"] == "structured_tool"
    assert kinds["DeleteRecordTool"] == "basetool_subclass"


def test_hints_are_offered_and_labelled_as_hints(tmp_path):
    plan = analyse(write_project(tmp_path))
    hints = {c.name: c.hint for c in plan.candidates}

    assert "send" in hints["send_welcome_email"]
    assert "IRREVERSIBLE" in hints["send_welcome_email"]
    assert "charge" in hints["charge_customer"]
    assert hints["search_docs"] is None            # a read needs no warning

    # every hint says it is a hint -- a hint that reads like a verdict is a
    # guess wearing a hat
    for hint in (h for h in hints.values() if h):
        assert "not a classification" in hint


def test_a_project_with_no_tools_says_so_rather_than_inventing_work(tmp_path):
    (tmp_path / "m.py").write_text("x = 1\n", encoding="utf-8")
    plan = analyse(tmp_path)
    assert plan.candidates == ()
    assert "nothing here to protect" in plan.format_text()


def test_an_unparseable_file_does_not_stop_the_scan(tmp_path):
    """Real projects have a broken file. Refusing to help at all would be
    worse than scanning what parses."""
    write_project(tmp_path)
    (tmp_path / "app" / "broken.py").write_text("def nope(:\n", encoding="utf-8")

    plan = analyse(tmp_path)
    assert len(plan.candidates) == 4               # the good files still scanned


# -- the generated module refuses to run undecided ------------------------------------

def test_the_generated_module_leaves_semantics_undecided(tmp_path):
    plan = analyse(write_project(tmp_path))
    module = plan.render_module()

    assert module.count("semantics=DECIDE") == 4
    for name in ("search_docs", "send_welcome_email", "charge_customer",
                 "DeleteRecordTool"):
        assert name in module
    # the hints travel into the file, where the decision is actually made
    assert "no automated undo exists" in module


def test_the_decide_sentinel_raises_rather_than_defaulting(tmp_path):
    """The load-bearing property: an unanswered semantics cannot be run past.
    A default here would silently classify somebody's side effects."""
    plan = analyse(write_project(tmp_path))
    target = tmp_path / "saga_tools.py"
    target.write_text(plan.render_module(), encoding="utf-8")

    # It turns out to fail even earlier than designed: `fleet.register` reads
    # `semantics.value`, so the module cannot be IMPORTED while any decision is
    # outstanding. That is the stronger guarantee, so it is what gets asserted.
    probe = tmp_path / "probe.py"
    probe.write_text(
        "try:\n"
        "    import saga_tools\n"
        "except RuntimeError as exc:\n"
        "    print('RAISED:', 'semantics' in str(exc))\n"
        "else:\n"
        "    print('IMPORTED WITH UNDECIDED SEMANTICS')\n",
        encoding="utf-8")

    # Point the child at THIS checkout rather than whatever is installed:
    # a stale wheel in site-packages would otherwise decide the result.
    import os

    env = {**os.environ,
           "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
    result = subprocess.run([sys.executable, "probe.py"], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr
    assert "RAISED: True" in result.stdout


def test_the_generated_module_is_valid_python(tmp_path):
    import ast

    plan = analyse(write_project(tmp_path))
    ast.parse(plan.render_module())


# -- the CLI ----------------------------------------------------------------------------

def test_adopt_cli_reports_and_writes(tmp_path, capsys):
    from agent_saga.cli import main

    write_project(tmp_path)
    assert main(["adopt", "--root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "frameworks detected : crewai, langchain" in out
    assert "tools found         : 4" in out

    target = tmp_path / "saga_tools.py"
    assert main(["adopt", "--root", str(tmp_path), "--out", str(target)]) == 0
    assert "semantics=DECIDE" in target.read_text(encoding="utf-8")

    # and it will not clobber a file someone has since filled in
    assert main(["adopt", "--root", str(tmp_path), "--out", str(target)]) == 1
    assert "--force" in capsys.readouterr().out


def test_adopt_cli_on_a_project_with_nothing_to_wrap(tmp_path, capsys):
    from agent_saga.cli import main

    (tmp_path / "m.py").write_text("x = 1\n", encoding="utf-8")
    assert main(["adopt", "--root", str(tmp_path),
                 "--out", str(tmp_path / "out.py")]) == 0
    assert "Nothing to write" in capsys.readouterr().out
    assert not (tmp_path / "out.py").exists()
