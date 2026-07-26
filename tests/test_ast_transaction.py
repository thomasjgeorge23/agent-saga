"""Codemod-as-transaction: the four promises, each with a test that would fail
if the engine broke it.

1. Broken output never reaches the host: staged source must parse AND compile.
2. A veto (shadow rejection or pre-verifier) leaves the tree byte-identical
   and writes no intent to the WAL -- no side effect, no intent owed.
3. A post-verifier failure rolls the files back through the saga, exactly like
   a failed charge.
4. The restore handler is idempotent, proves snapshot integrity before writing
   anything back, and treats an absent manifest as proof of a no-op.
"""

import ast
import json
from pathlib import Path

import pytest
from conftest import aio

from agent_saga import AsyncWAL, SagaAborted, saga_scope
from agent_saga.codemod import (
    AstTransaction,
    RestoreIntegrityError,
    ShadowRejected,
    ShadowTree,
    VerificationFailed,
    restore_files,
)

CONFIG = "# tuning knobs\nTIMEOUT = 30\nRETRIES = 2\n"
SERVICE = "def window() -> int:\n    return TIMEOUT\n\n\nTIMEOUT = 30\n"


def make_project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "config.py").write_text(CONFIG, encoding="utf-8")
    (root / "pkg" / "service.py").write_text(SERVICE, encoding="utf-8")
    return root


def snapshot_bytes(root: Path) -> dict:
    """Byte-exact view of the project files -- excluding the engine's own
    snapshot artifacts, which are rollback state, not project state."""
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob("*.py")
            if ".agent_saga_snapshots" not in p.relative_to(root).parts}


def bump_timeout(module):
    """AST-guided transform: only touch modules that really assign TIMEOUT."""
    for node in ast.walk(module.tree):
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", None) == "TIMEOUT" for t in node.targets)):
            return module.source.replace("TIMEOUT = 30", "TIMEOUT = 45")
    return None


# -- promise 1: broken output never lands ---------------------------------------

def test_transform_output_that_does_not_parse_is_rejected(tmp_path):
    root = make_project(tmp_path)
    before = snapshot_bytes(root)
    tx = AstTransaction(root)
    tree = tx.shadow()

    with pytest.raises(ShadowRejected) as excinfo:
        tree.apply(lambda m: "def broken(:\n")
    # every failing file is reported, not just the first
    assert len(excinfo.value.failures) == 2
    assert snapshot_bytes(root) == before


def test_compile_gate_catches_what_the_grammar_allows(tmp_path):
    """`nonlocal x` at module level parses in some interpreters' ast layer but
    can never compile; either stage must reject it -- a green check next to a
    file the interpreter refuses to import is the lie this gate exists for."""
    root = make_project(tmp_path)
    tx = AstTransaction(root)
    tree = tx.shadow(["pkg/config.py"])

    with pytest.raises(ShadowRejected):
        tree.apply(lambda m: "nonlocal x\n")
    assert (root / "pkg" / "config.py").read_text(encoding="utf-8") == CONFIG


def test_an_input_file_that_does_not_parse_fails_loudly_up_front(tmp_path):
    root = make_project(tmp_path)
    (root / "pkg" / "broken.py").write_text("def nope(:\n", encoding="utf-8")

    with pytest.raises(ShadowRejected) as excinfo:
        ShadowTree.load(root, ["pkg/broken.py", "pkg/config.py"])
    assert "broken.py" in str(excinfo.value)


# -- promise 2: a veto leaves the host untouched --------------------------------

@aio
async def test_pre_verifier_veto_touches_nothing_and_logs_no_intent(tmp_path):
    root = make_project(tmp_path)
    before = snapshot_bytes(root)

    def veto(_tree):
        raise RuntimeError("type check failed in the shadow")

    tx = AstTransaction(root, verifiers=[veto])
    tree = tx.shadow()
    assert tree.apply(bump_timeout) == 2

    wal = AsyncWAL(tmp_path / "wal.jsonl")
    await wal.start()
    try:
        with pytest.raises(SagaAborted) as excinfo:
            async with saga_scope(wal=wal) as ctx:
                await tx.commit(ctx, tree)
        assert isinstance(excinfo.value.__cause__, VerificationFailed)

        assert snapshot_bytes(root) == before
        records = await wal.read_all()
        assert not [r for r in records
                    if r.get("event") == "STEP_INTENT" and r.get("tool") == "codemod.apply"]
    finally:
        await wal.close()


@aio
async def test_a_no_op_codemod_reports_itself_as_a_no_op(tmp_path):
    root = make_project(tmp_path)
    tx = AstTransaction(root)
    tree = tx.shadow()
    tree.apply(lambda m: None)

    wal = AsyncWAL(tmp_path / "wal.jsonl")
    await wal.start()
    try:
        async with saga_scope(wal=wal) as ctx:
            result = await tx.commit(ctx, tree)
        assert result.applied is False and result.manifest is None
    finally:
        await wal.close()


# -- the happy path, with a recoverable compensation on the record --------------

@aio
async def test_commit_applies_files_and_records_a_recoverable_compensation(tmp_path):
    root = make_project(tmp_path)
    tx = AstTransaction(root)
    tree = tx.shadow()
    assert tree.apply(bump_timeout) == 2

    wal = AsyncWAL(tmp_path / "wal.jsonl")
    await wal.start()
    try:
        async with saga_scope(wal=wal) as ctx:
            result = await tx.commit(ctx, tree)

        assert result.applied is True
        assert "TIMEOUT = 45" in (root / "pkg" / "config.py").read_text(encoding="utf-8")
        assert "TIMEOUT = 45" in (root / "pkg" / "service.py").read_text(encoding="utf-8")
        assert Path(result.manifest).exists()

        committed = [r for r in await wal.read_all()
                     if r.get("event") == "STEP_COMMITTED" and r.get("tool") == "codemod.apply"]
        assert len(committed) == 1
        comp = committed[0]["compensation"]
        # the part a recovery daemon in another process depends on:
        assert comp["handler"] == "codemod.restore_files"
        assert comp["recoverable"] is True
    finally:
        await wal.close()


# -- promise 3: post-verify failure rolls the write back ------------------------

@aio
async def test_post_verifier_failure_restores_every_byte(tmp_path):
    root = make_project(tmp_path)
    before = snapshot_bytes(root)

    def sad_after(_tree):
        raise RuntimeError("smoke test failed against the real tree")

    tx = AstTransaction(root, post_verifiers=[sad_after])
    tree = tx.shadow()
    tree.apply(bump_timeout)

    wal = AsyncWAL(tmp_path / "wal.jsonl")
    await wal.start()
    try:
        with pytest.raises(SagaAborted):
            async with saga_scope(wal=wal) as ctx:
                await tx.commit(ctx, tree)

        assert snapshot_bytes(root) == before
        events = {r.get("event") for r in await wal.read_all()}
        assert "ROLLBACK_START" in events
    finally:
        await wal.close()


# -- promise 4: the restore handler -----------------------------------------------

@aio
async def test_restore_handler_is_cross_process_shaped_and_idempotent(tmp_path):
    """The daemon has only the manifest path (JSON kwargs) and the registry
    name. Calling the handler exactly as the daemon would must restore the
    tree, and calling it twice must be harmless."""
    root = make_project(tmp_path)
    before = snapshot_bytes(root)
    tx = AstTransaction(root)
    tree = tx.shadow()
    tree.apply(bump_timeout)

    wal = AsyncWAL(tmp_path / "wal.jsonl")
    await wal.start()
    try:
        async with saga_scope(wal=wal) as ctx:
            result = await tx.commit(ctx, tree)
    finally:
        await wal.close()

    report = restore_files(manifest=result.manifest)
    assert report["restored"] == 2
    assert snapshot_bytes(root) == before

    report = restore_files(manifest=result.manifest)   # idempotent
    assert report["restored"] == 2
    assert snapshot_bytes(root) == before


@aio
async def test_restore_refuses_a_tampered_snapshot(tmp_path):
    root = make_project(tmp_path)
    tx = AstTransaction(root)
    tree = tx.shadow(["pkg/config.py"])
    tree.apply(bump_timeout)

    wal = AsyncWAL(tmp_path / "wal.jsonl")
    await wal.start()
    try:
        async with saga_scope(wal=wal) as ctx:
            result = await tx.commit(ctx, tree)
    finally:
        await wal.close()

    manifest = json.loads(Path(result.manifest).read_text("utf-8"))
    snap = Path(result.manifest).parent / manifest["entries"][0]["snapshot"]
    snap.write_bytes(b"# not what was recorded\n")

    with pytest.raises(RestoreIntegrityError) as excinfo:
        restore_files(manifest=result.manifest)
    assert "config.py" in str(excinfo.value)
    # the corrupted snapshot was NOT written over the live file
    assert "TIMEOUT = 45" in (root / "pkg" / "config.py").read_text(encoding="utf-8")


def test_absent_manifest_is_proof_of_a_no_op(tmp_path):
    report = restore_files(manifest=str(tmp_path / "never" / "manifest.json"))
    assert report["restored"] == 0
    assert "no host file was mutated" in report["reason"]


@aio
async def test_created_files_are_deleted_on_restore(tmp_path):
    root = make_project(tmp_path)
    tx = AstTransaction(root)
    tree = tx.shadow()
    tree.create("pkg/generated.py", "GENERATED = True\n")

    wal = AsyncWAL(tmp_path / "wal.jsonl")
    await wal.start()
    try:
        async with saga_scope(wal=wal) as ctx:
            result = await tx.commit(ctx, tree)
    finally:
        await wal.close()

    assert (root / "pkg" / "generated.py").exists()
    report = restore_files(manifest=result.manifest)
    assert report["deleted"] == 1
    assert not (root / "pkg" / "generated.py").exists()


# -- encodings are preserved, not "fixed" ----------------------------------------

@aio
async def test_non_utf8_files_round_trip_in_their_own_encoding(tmp_path):
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    original = "# -*- coding: latin-1 -*-\nNAME = 'caf\xe9'\nTIMEOUT = 30\n"
    (root / "pkg" / "legacy.py").write_bytes(original.encode("latin-1"))

    tx = AstTransaction(root)
    tree = tx.shadow()
    assert tree.apply(bump_timeout) == 1

    wal = AsyncWAL(tmp_path / "wal.jsonl")
    await wal.start()
    try:
        async with saga_scope(wal=wal) as ctx:
            await tx.commit(ctx, tree)
    finally:
        await wal.close()

    raw = (root / "pkg" / "legacy.py").read_bytes()
    assert b"caf\xe9" in raw            # still latin-1 bytes, not re-encoded utf-8
    assert b"TIMEOUT = 45" in raw
