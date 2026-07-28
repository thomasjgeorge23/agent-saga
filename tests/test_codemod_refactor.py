"""Codemod stages 1-3: index, plan, gate. The stages DESIGN_SPEC promised.

A rename that catches a local variable, a string, or an unrelated object's
attribute is not a refactor -- it is corruption that compiles. So most of what
follows is adversarial: the cases where a naive find-and-replace is wrong.

The claims under test:
1. Resolution is scope-correct: locals, parameters, comprehensions, lambdas,
   class bodies, and strings shadow or exclude the module-level symbol;
   `global` re-includes it.
2. Cross-module references resolve through the import graph -- bare name,
   dotted attribute, and the name inside `from x import y` -- while an aliased
   import rewrites only the import.
3. The plan refuses rather than guesses: name collisions, invalid identifiers,
   unknown symbols, overlapping edits.
4. Splicing preserves everything it did not target, byte-for-byte.
5. The blast radius drives the gate, and unresolved references widen it.
6. Unused-import removal is conservative about the imports that are load
   bearing.
7. It flows into the existing transaction, so a failed verification restores
   the tree.
"""

import ast
from pathlib import Path

import pytest
from conftest import aio

from agent_saga import AsyncWAL, SagaAborted, saga_scope
from agent_saga.codemod.ast_transaction import AstTransaction
from agent_saga.codemod.index import IndexError_, SymbolIndex
from agent_saga.codemod.plan import (
    PlanError,
    plan_transform,
    remove_unused_imports,
    rename_symbol,
)


def make_pkg(root: Path, files: dict) -> Path:
    """Write files with exact bytes. `newline=""` matters: without it,
    Python translates every newline on Windows, so assertions about the
    output would be testing the platform rather than the codemod."""
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
    return root


def renamed(root: Path, qualname: str, new: str) -> dict:
    """{rel path: source after the rename}, including files the plan did NOT
    touch -- a test that a file was left alone needs to read it too."""
    index = SymbolIndex.build(root)
    plan = rename_symbol(index, qualname, new)
    out = {}
    for module, info in index.modules.items():
        rel = info.path.relative_to(index.root).as_posix()
        out[rel] = plan.new_source(rel) or info.source
    return out


# -- 1. scope correctness -------------------------------------------------------------

def test_a_local_binding_shadows_the_module_symbol(tmp_path):
    make_pkg(tmp_path, {
        "m.py": (
            "def helper():\n"
            "    return 1\n"
            "\n"
            "def uses():\n"
            "    return helper()\n"
            "\n"
            "def shadows():\n"
            "    helper = 2\n"
            "    return helper\n"
        )})
    out = renamed(tmp_path, "m.helper", "compute")["m.py"]
    assert "def compute():" in out
    assert "return compute()" in out
    assert "    helper = 2\n    return helper" in out      # untouched


def test_a_parameter_shadows_the_module_symbol(tmp_path):
    make_pkg(tmp_path, {
        "m.py": "def helper():\n    return 1\n\ndef f(helper):\n    return helper\n"})
    out = renamed(tmp_path, "m.helper", "compute")["m.py"]
    assert "def f(helper):\n    return helper" in out


def test_a_global_declaration_re_includes_the_module_symbol(tmp_path):
    make_pkg(tmp_path, {
        "m.py": (
            "helper = 1\n"
            "\n"
            "def rebind():\n"
            "    global helper\n"
            "    helper = 2\n"
            "    return helper\n"
        )})
    out = renamed(tmp_path, "m.helper", "value")["m.py"]
    assert "global value" in out
    assert "    value = 2" in out
    assert "return value" in out
    assert "helper" not in out


def test_comprehension_and_lambda_bindings_shadow(tmp_path):
    make_pkg(tmp_path, {
        "m.py": (
            "def helper():\n"
            "    return 1\n"
            "\n"
            "squares = [helper for helper in range(3)]\n"
            "f = lambda helper: helper + 1\n"
            "used = helper()\n"
        )})
    out = renamed(tmp_path, "m.helper", "compute")["m.py"]
    assert "[helper for helper in range(3)]" in out         # comprehension scope
    assert "lambda helper: helper + 1" in out               # lambda parameter
    assert "used = compute()" in out                        # the real reference


def test_a_class_attribute_of_the_same_name_is_not_touched(tmp_path):
    make_pkg(tmp_path, {
        "m.py": (
            "def helper():\n"
            "    return 1\n"
            "\n"
            "class Thing:\n"
            "    helper = 'attribute'\n"
            "\n"
            "    def method(self):\n"
            "        return helper()\n"
        )})
    out = renamed(tmp_path, "m.helper", "compute")["m.py"]
    assert "    helper = 'attribute'" in out
    # a method body does NOT see the class attribute, so this IS the module one
    assert "return compute()" in out


def test_strings_and_unrelated_attributes_are_never_touched(tmp_path):
    make_pkg(tmp_path, {
        "m.py": (
            "def helper():\n"
            "    return 1\n"
            "\n"
            "note = 'call helper() first'\n"
            "other = some_object.helper\n"
            "real = helper()\n"
        )})
    out = renamed(tmp_path, "m.helper", "compute")["m.py"]
    assert "'call helper() first'" in out
    assert "some_object.helper" in out
    assert "real = compute()" in out


# -- 2. cross-module resolution ----------------------------------------------------------

def test_all_three_reference_kinds_are_rewritten(tmp_path):
    make_pkg(tmp_path, {
        "pkg/__init__.py": "",
        "pkg/util.py": "def helper(x):\n    return x\n",
        "pkg/app.py": (
            "from pkg.util import helper\n"
            "import pkg.util\n"
            "\n"
            "def run():\n"
            "    return helper(1) + pkg.util.helper(2)\n"
        )})
    out = renamed(tmp_path, "pkg.util.helper", "compute")
    assert "def compute(x):" in out["pkg/util.py"]
    assert "from pkg.util import compute" in out["pkg/app.py"]
    assert "return compute(1) + pkg.util.compute(2)" in out["pkg/app.py"]


def test_an_aliased_import_rewrites_the_import_and_not_the_call_sites(tmp_path):
    make_pkg(tmp_path, {
        "pkg/__init__.py": "",
        "pkg/util.py": "def helper(x):\n    return x\n",
        "pkg/app.py": "from pkg.util import helper as h\n\ndef run():\n    return h(1)\n",
    })
    out = renamed(tmp_path, "pkg.util.helper", "compute")
    assert "from pkg.util import compute as h" in out["pkg/app.py"]
    assert "return h(1)" in out["pkg/app.py"]        # call sites say h, and still do


def test_a_module_alias_resolves_dotted_references(tmp_path):
    make_pkg(tmp_path, {
        "pkg/__init__.py": "",
        "pkg/util.py": "def helper(x):\n    return x\n",
        "pkg/app.py": "import pkg.util as u\n\ndef run():\n    return u.helper(1)\n",
    })
    out = renamed(tmp_path, "pkg.util.helper", "compute")
    assert "return u.compute(1)" in out["pkg/app.py"]


def test_relative_imports_resolve(tmp_path):
    make_pkg(tmp_path, {
        "pkg/__init__.py": "",
        "pkg/util.py": "def helper(x):\n    return x\n",
        "pkg/app.py": "from .util import helper\n\ndef run():\n    return helper(1)\n",
    })
    out = renamed(tmp_path, "pkg.util.helper", "compute")
    assert "from .util import compute" in out["pkg/app.py"]
    assert "return compute(1)" in out["pkg/app.py"]


def test_a_local_variable_shadowing_a_module_alias_is_not_followed(tmp_path):
    make_pkg(tmp_path, {
        "pkg/__init__.py": "",
        "pkg/util.py": "def helper(x):\n    return x\n",
        "pkg/app.py": (
            "import pkg.util\n"
            "\n"
            "def run(pkg):\n"
            "    return pkg.util.helper(1)\n"     # `pkg` is the PARAMETER here
        )})
    out = renamed(tmp_path, "pkg.util.helper", "compute")
    assert "return pkg.util.helper(1)" in out["pkg/app.py"]


# -- 3. refusals -------------------------------------------------------------------------

def test_renaming_into_an_existing_name_is_refused(tmp_path):
    make_pkg(tmp_path, {"m.py": "def helper():\n    pass\n\ndef compute():\n    pass\n"})
    index = SymbolIndex.build(tmp_path)
    with pytest.raises(PlanError, match="already exists"):
        rename_symbol(index, "m.helper", "compute")


@pytest.mark.parametrize("bad", ["class", "9lives", "has space", ""])
def test_an_invalid_identifier_is_refused(tmp_path, bad):
    make_pkg(tmp_path, {"m.py": "def helper():\n    pass\n"})
    index = SymbolIndex.build(tmp_path)
    with pytest.raises(PlanError, match="identifier"):
        rename_symbol(index, "m.helper", bad)


def test_an_unknown_symbol_names_what_is_available(tmp_path):
    make_pkg(tmp_path, {"m.py": "def helper():\n    pass\n"})
    index = SymbolIndex.build(tmp_path)
    with pytest.raises(IndexError_, match="no definition"):
        rename_symbol(index, "m.nonexistent", "x")


def test_an_unparseable_file_fails_the_index_rather_than_being_skipped(tmp_path):
    """Skipping it would compute a blast radius that is too small -- the one
    direction that matters."""
    make_pkg(tmp_path, {"m.py": "def helper():\n    pass\n",
                        "broken.py": "def nope(:\n"})
    with pytest.raises(IndexError_, match="broken.py"):
        SymbolIndex.build(tmp_path)


# -- 4. splicing preserves everything else -------------------------------------------------

def test_comments_formatting_and_encoding_survive_byte_for_byte(tmp_path):
    source = (
        "# -*- coding: latin-1 -*-\n"
        "# a comment mentioning helper, which must NOT change\n"
        "NAME = 'caf\xe9'\n"
        "\n"
        "\n"
        "def helper( x ,  y ):      # odd spacing kept\n"
        "    '''docstring naming helper'''\n"
        "    return x\n"
    )
    (tmp_path / "m.py").write_bytes(source.encode("latin-1"))

    index = SymbolIndex.build(tmp_path)
    out = rename_symbol(index, "m.helper", "compute").new_source("m.py")

    assert "# a comment mentioning helper, which must NOT change" in out
    assert "'''docstring naming helper'''" in out
    assert "def compute( x ,  y ):      # odd spacing kept" in out
    assert "caf\xe9" in out
    assert out.count("\n\n\n") == 1                      # blank lines preserved


def test_non_ascii_before_an_identifier_does_not_shift_the_span(tmp_path):
    """ast reports columns in UTF-8 bytes; a naive char-offset conversion
    corrupts any line with non-ASCII text before the identifier."""
    make_pkg(tmp_path, {
        "m.py": "def helper():\n    return 1\n\nx = ['éééé', helper()]\n"})
    out = renamed(tmp_path, "m.helper", "compute")["m.py"]
    assert "x = ['éééé', compute()]" in out


def test_the_result_still_parses(tmp_path):
    make_pkg(tmp_path, {
        "pkg/__init__.py": "",
        "pkg/util.py": "def helper(x):\n    return x\n",
        "pkg/app.py": "from pkg.util import helper\nv = helper(1)\n"})
    for path, source in renamed(tmp_path, "pkg.util.helper", "compute").items():
        ast.parse(source, filename=path)


# -- 5. blast radius drives the gate ----------------------------------------------------

def test_a_private_single_module_rename_needs_no_review(tmp_path):
    make_pkg(tmp_path, {"m.py": "def _helper():\n    return 1\n\nv = _helper()\n"})
    index = SymbolIndex.build(tmp_path)
    plan = rename_symbol(index, "m._helper", "_compute")
    assert not plan.blast_radius.is_public
    assert plan.blast_radius.modules == ("m",)
    assert not plan.requires_review


def test_a_public_or_cross_module_rename_requires_review(tmp_path):
    make_pkg(tmp_path, {
        "pkg/__init__.py": "",
        "pkg/util.py": "def helper():\n    return 1\n",
        "pkg/app.py": "from pkg.util import helper\nv = helper()\n"})
    index = SymbolIndex.build(tmp_path)
    plan = rename_symbol(index, "pkg.util.helper", "compute")
    assert plan.blast_radius.is_public
    assert len(plan.blast_radius.modules) == 2
    assert plan.requires_review


def test_a_star_import_is_recorded_and_widens_the_radius(tmp_path):
    make_pkg(tmp_path, {
        "pkg/__init__.py": "",
        "pkg/util.py": "def _helper():\n    return 1\n",
        "pkg/app.py": "from pkg.util import *\n"})
    index = SymbolIndex.build(tmp_path)
    assert any(u.reason == "star-import" for u in index.unresolved)

    plan = rename_symbol(index, "pkg.util._helper", "_compute")
    # private + single defining module, but a star import elsewhere means the
    # index cannot prove completeness, so review is required anyway
    assert plan.requires_review


def test_dynamic_access_is_recorded_as_unresolved(tmp_path):
    make_pkg(tmp_path, {
        "m.py": "def _helper():\n    return 1\n\nf = globals()['_helper']\n"})
    index = SymbolIndex.build(tmp_path)
    assert any(u.reason.startswith("dynamic-") for u in index.unresolved)
    assert rename_symbol(index, "m._helper", "_compute").requires_review


# -- 6. conservative unused-import removal -------------------------------------------------

def test_unused_imports_are_removed_and_used_ones_kept(tmp_path):
    make_pkg(tmp_path, {
        "m.py": "import os\nimport sys\n\nprint(sys.argv)\n"})
    index = SymbolIndex.build(tmp_path)
    out = remove_unused_imports(index).new_source("m.py")
    assert "import os" not in out
    assert "import sys" in out
    assert "print(sys.argv)" in out


def test_load_bearing_imports_are_kept(tmp_path):
    make_pkg(tmp_path, {
        "m.py": (
            "import logging.config\n"          # dotted, side-effecting
            "from typing import List\n"        # used only in a string annotation
            "import exported\n"
            "\n"
            "try:\n"
            "    import ujson\n"               # guarded probe
            "except ImportError:\n"
            "    ujson = None\n"
            "\n"
            "__all__ = ['exported']\n"
            "\n"
            "def f(x: 'List[int]'):\n"
            "    return x\n"
        )})
    index = SymbolIndex.build(tmp_path)
    plan = remove_unused_imports(index)
    out = plan.new_source("m.py") or index.source_of("m")
    assert "import logging.config" in out
    assert "from typing import List" in out
    assert "import exported" in out
    assert "import ujson" in out


def test_one_dead_name_out_of_several_takes_its_comma(tmp_path):
    make_pkg(tmp_path, {"m.py": "from os import path, sep, curdir\n\nprint(path, sep)\n"})
    index = SymbolIndex.build(tmp_path)
    out = remove_unused_imports(index).new_source("m.py")
    assert "from os import path, sep\n" in out
    ast.parse(out)


def test_init_files_are_left_alone(tmp_path):
    make_pkg(tmp_path, {"pkg/__init__.py": "from pkg.util import helper\n",
                        "pkg/util.py": "def helper():\n    return 1\n"})
    index = SymbolIndex.build(tmp_path)
    assert remove_unused_imports(index).is_empty


# -- 7. it flows into the transaction ------------------------------------------------------

@aio
async def test_a_plan_applies_transactionally_and_rolls_back(tmp_path):
    root = tmp_path / "proj"
    make_pkg(root, {
        "pkg/__init__.py": "",
        "pkg/util.py": "def helper(x):\n    return x\n",
        "pkg/app.py": "from pkg.util import helper\nv = helper(1)\n"})
    before = {p.relative_to(root).as_posix(): p.read_bytes()
              for p in root.rglob("*.py")}

    index = SymbolIndex.build(root)
    plan = rename_symbol(index, "pkg.util.helper", "compute")

    def veto(_tree):
        raise RuntimeError("the type checker said no")

    wal = AsyncWAL(tmp_path / "wal.jsonl")
    await wal.start()
    try:
        transaction = AstTransaction(root, post_verifiers=[veto])
        tree = transaction.shadow(plan.paths)
        assert tree.apply(plan_transform(plan)) == 2

        with pytest.raises(SagaAborted):
            async with saga_scope(wal=wal) as ctx:
                await transaction.commit(ctx, tree)
    finally:
        await wal.close()

    after = {p.relative_to(root).as_posix(): p.read_bytes()
             for p in root.rglob("*.py")
             if ".agent_saga_snapshots" not in p.relative_to(root).parts}
    assert after == before


# -- the CLI ------------------------------------------------------------------------------

def test_refactor_cli_is_dry_run_by_default_and_gates_on_review(tmp_path, capsys):
    from agent_saga.cli import main

    root = tmp_path / "proj"
    make_pkg(root, {
        "pkg/__init__.py": "",
        "pkg/util.py": "def helper(x):\n    return x\n",
        "pkg/app.py": "from pkg.util import helper\nv = helper(1)\n"})
    original = (root / "pkg" / "util.py").read_text(encoding="utf-8")

    assert main(["refactor", "rename", "--symbol", "pkg.util.helper",
                 "--to", "compute", "--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "-def helper(x):" in out and "+def compute(x):" in out
    assert "Dry run" in out
    assert (root / "pkg" / "util.py").read_text(encoding="utf-8") == original

    # --apply without --yes is refused for a plan that needs review
    assert main(["refactor", "rename", "--symbol", "pkg.util.helper", "--to",
                 "compute", "--root", str(root), "--apply"]) == 1
    assert "needs review" in capsys.readouterr().out
    assert (root / "pkg" / "util.py").read_text(encoding="utf-8") == original


def test_refactor_cli_applies_with_yes(tmp_path, capsys):
    from agent_saga.cli import main

    root = tmp_path / "proj"
    make_pkg(root, {
        "pkg/__init__.py": "",
        "pkg/util.py": "def helper(x):\n    return x\n",
        "pkg/app.py": "from pkg.util import helper\nv = helper(1)\n"})

    assert main(["refactor", "rename", "--symbol", "pkg.util.helper", "--to",
                 "compute", "--root", str(root), "--apply", "--yes",
                 "--wal", str(tmp_path / "w.wal")]) == 0
    assert "applied to 2 file(s)" in capsys.readouterr().out
    assert "def compute(x):" in (root / "pkg" / "util.py").read_text(encoding="utf-8")


def test_refactor_cli_reports_a_bad_rename_without_a_traceback(tmp_path, capsys):
    from agent_saga.cli import main

    root = tmp_path / "proj"
    make_pkg(root, {"m.py": "def helper():\n    pass\n\ndef compute():\n    pass\n"})
    assert main(["refactor", "rename", "--symbol", "m.helper", "--to", "compute",
                 "--root", str(root)]) == 1
    assert "already exists" in capsys.readouterr().out
