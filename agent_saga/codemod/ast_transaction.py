"""Codemod-as-transaction: repository edits with the same guarantees as a charge.

A multi-file refactor is a side-effect-heavy, partially-committed operation --
exactly the shape of failure the saga core exists for. This module treats a
codebase modification the way `context.py` treats a Stripe charge:

  1. **Shadow first.** Every transform runs against an in-memory copy of the
     tree. Each staged result must reparse (`ast.parse`) *and* compile to
     bytecode (`compile(tree, ...)` -- which catches errors the grammar allows,
     like a module-level ``nonlocal``). A transform that produces broken code
     is rejected while the host filesystem is still byte-identical.

  2. **Verification before mutation.** Pre-verifiers (type checkers, test runs
     against a materialized copy of the shadow) veto the commit before any host
     file is touched. Refusal at this point is free -- the same doctrine as the
     pre-flight gate.

  3. **One transactional write step.** The actual mutation is a single
     `ctx.execute(...)` with ``COMPENSABLE`` semantics. Not ``REVERSIBLE``:
     files are durable state, and `snapshot.py` is explicit that durable state
     needs a registry-backed handler a *recovery daemon in another process* can
     run -- an in-process closure cannot survive a SIGKILL.

  4. **Durability ordering, applied to files.** Before the first target file is
     mutated, the original bytes of every file are copied into a snapshot
     directory and a manifest is fsynced. The manifest's existence is therefore
     proof-carrying: if it is absent, the snapshot phase never completed, and
     the ordering guarantees no host file was mutated. The recovery handler
     relies on that certainty instead of guessing.

What this module deliberately does NOT claim:

  * **No cross-file atomicity.** Each file is replaced atomically (temp file +
    fsync + ``os.replace``), but a crash mid-commit leaves a mixed tree. That
    is precisely why the snapshot+manifest is made durable *first* and the
    compensation is registry-backed: ``codemod.restore_files`` restores every
    file in the manifest, from any process, at any later time.
  * **No lossless AST rewriting.** Transforms receive the parsed AST for
    analysis but return *source text*. The stdlib unparser discards comments
    and formatting, so an engine that silently round-tripped through
    ``ast.unparse`` would vandalize every file it "fixed". A transform that
    wants surgical edits should splice text using the node positions on the
    tree it is handed.
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import logging
import os
import tokenize
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import (Any, Awaitable, Callable, Dict, Iterable, List, Mapping,
                    Optional, Protocol, Sequence, Tuple, Union)

from ..registry import compensator
from ..semantics import ActionSemantics, Compensation

logger = logging.getLogger("agent_saga.codemod")

MANIFEST_NAME = "manifest.json"

_EXCLUDED_DIRS = {".git", "__pycache__", ".agent_saga_snapshots", ".venv", "venv",
                  "node_modules", ".pytest_cache"}


class CodemodError(RuntimeError):
    """Base class for every failure this module raises. Nothing here fails
    silently; if you see a bare CodemodError subclass, no host file is in an
    undocumented state."""


class ShadowRejected(CodemodError):
    """A transform produced source that does not parse or compile, or an input
    file was broken before we started. Raised entirely in the shadow phase:
    the host filesystem is untouched."""

    def __init__(self, failures: Sequence[Tuple[str, BaseException]]):
        self.failures = list(failures)
        lines = [f"  {rel}: {exc!r}" for rel, exc in self.failures]
        super().__init__(
            "shadow tree rejected; the host filesystem was not touched:\n"
            + "\n".join(lines)
        )


class VerificationFailed(CodemodError):
    """A verification hook vetoed the codemod. If it was a pre-verifier the
    host is untouched; if it was a post-verifier the surrounding saga is
    rolling the files back as this propagates."""


class RestoreIntegrityError(CodemodError):
    """The snapshot a restore depends on is missing, corrupted, or escapes the
    project root. Restoring wrong bytes silently would be worse than halting,
    so this halts -- the daemon records COMPENSATION_FAILED and a human looks."""


@dataclass
class ShadowModule:
    """One tracked file: its on-disk identity plus the in-memory working copy."""

    rel: str                      # posix-style path relative to the root
    path: Path                    # absolute
    encoding: str                 # from the PEP 263 cookie / BOM, or utf-8
    original_bytes: Optional[bytes]   # None => file did not exist (created)
    original_source: str
    source: str                   # current shadow state (staged transforms applied)
    tree: ast.Module              # parse of `source`, kept in lockstep

    @property
    def changed(self) -> bool:
        return self.original_bytes is None or self.source != self.original_source


class Transform(Protocol):
    """A codemod pass. Receives one module (source + parsed AST for analysis),
    returns replacement source text, or None for "no change".

    The engine, not the transform, is responsible for proving the result still
    compiles -- a transform cannot ship broken code no matter what it returns.
    """

    def __call__(self, module: ShadowModule) -> Optional[str]: ...


Verifier = Callable[["ShadowTree"], Union[None, Awaitable[None]]]
"""A hook that raises to veto. Sync or async. Pre-verifiers run against the
shadow (use `materialize()` to hand real tools a real directory); post-verifiers
run after the host files are written, inside the saga, so raising rolls the
write back."""


class ShadowTree:
    """The in-memory working copy of the files a codemod may touch."""

    def __init__(self, root: Path, modules: Dict[str, ShadowModule]):
        self.root = root
        self._modules: Dict[str, ShadowModule] = modules

    # -- construction --------------------------------------------------------

    @classmethod
    def load(cls, root: Union[str, Path], paths: Iterable[Union[str, Path]]) -> "ShadowTree":
        """Read and parse every path. A file that does not parse *before* any
        transform runs is an input error, reported per-file -- never a state we
        discover later with half a plan applied."""
        root_p = Path(root).resolve()
        modules: Dict[str, ShadowModule] = {}
        failures: List[Tuple[str, BaseException]] = []

        for p in paths:
            abs_p = (root_p / p).resolve() if not Path(p).is_absolute() else Path(p).resolve()
            rel = _rel_or_raise(abs_p, root_p)
            if rel in modules:
                continue
            raw = abs_p.read_bytes()
            try:
                encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
                source = raw.decode(encoding)
                tree = ast.parse(source, filename=str(abs_p))
            except (SyntaxError, UnicodeDecodeError, ValueError) as exc:
                failures.append((rel, exc))
                continue
            modules[rel] = ShadowModule(
                rel=rel, path=abs_p, encoding=encoding,
                original_bytes=raw, original_source=source,
                source=source, tree=tree,
            )
        if failures:
            raise ShadowRejected(failures)
        return cls(root_p, modules)

    # -- shadow mutation ------------------------------------------------------

    def apply(self, transform: Transform) -> int:
        """Run one transform over every tracked module, staging its output.

        Every staged result must parse AND compile. Failures across the whole
        pass are collected and raised together, so a transform that breaks 7 of
        40 files reports all 7 -- not the first one, seven runs apart. Returns
        the number of modules changed by this pass.

        Modules are visited in sorted order and each staged result immediately
        replaces the module's shadow view, so a second `apply()` composes on
        top of the first deterministically.
        """
        failures: List[Tuple[str, BaseException]] = []
        staged: Dict[str, Tuple[str, ast.Module]] = {}

        for rel in sorted(self._modules):
            module = self._modules[rel]
            new_source = transform(module)
            if new_source is None or new_source == module.source:
                continue
            try:
                staged[rel] = (new_source, _prove_compiles(new_source, module.path))
            except (SyntaxError, ValueError) as exc:
                failures.append((rel, exc))

        if failures:
            raise ShadowRejected(failures)
        for rel, (new_source, tree) in staged.items():
            self._modules[rel].source = new_source
            self._modules[rel].tree = tree
        return len(staged)

    def create(self, rel: str, source: str, *, encoding: str = "utf-8") -> None:
        """Stage a brand-new file. Its compensation is deletion."""
        rel = rel.replace("\\", "/")
        abs_p = (self.root / rel).resolve()
        _rel_or_raise(abs_p, self.root)
        if rel in self._modules or abs_p.exists():
            raise CodemodError(f"create({rel!r}): the file already exists")
        tree = _prove_compiles(source, abs_p)   # raises before anything is staged
        self._modules[rel] = ShadowModule(
            rel=rel, path=abs_p, encoding=encoding,
            original_bytes=None, original_source="",
            source=source, tree=tree,
        )

    # -- inspection -----------------------------------------------------------

    @property
    def modules(self) -> Mapping[str, ShadowModule]:
        return dict(self._modules)

    @property
    def changed(self) -> Dict[str, ShadowModule]:
        return {rel: m for rel, m in self._modules.items() if m.changed}

    def materialize(self, dest: Union[str, Path]) -> Path:
        """Write the full shadow view (changed and unchanged files alike) into
        `dest`, so a pre-verifier can point mypy or pytest at a real directory
        that is not the host tree. Unchanged files are copied byte-exact."""
        dest_p = Path(dest)
        for rel, m in self._modules.items():
            target = dest_p / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if m.changed:
                target.write_bytes(m.source.encode(m.encoding))
            else:
                assert m.original_bytes is not None
                target.write_bytes(m.original_bytes)
        return dest_p


@dataclass(frozen=True)
class CodemodResult:
    applied: bool
    txn_id: str
    files: Tuple[str, ...]
    manifest: Optional[str]     # path to the durable restore manifest, if applied


class AstTransaction:
    """The engine. Dependency-injected: it owns no WAL and no saga -- it runs
    its single side-effecting step through the `SagaContext` you hand it, so a
    codemod composes with charges, tickets, and deploys in one transaction."""

    def __init__(
        self,
        root: Union[str, Path],
        *,
        snapshot_dir: Optional[Union[str, Path]] = None,
        verifiers: Sequence[Verifier] = (),
        post_verifiers: Sequence[Verifier] = (),
    ):
        self.root = Path(root).resolve()
        self.snapshot_dir = (Path(snapshot_dir).resolve() if snapshot_dir is not None
                             else self.root / ".agent_saga_snapshots" / "codemods")
        self.verifiers = tuple(verifiers)
        self.post_verifiers = tuple(post_verifiers)
        self.txn_id = uuid.uuid4().hex

    # -- construction helpers -------------------------------------------------

    def shadow(self, paths: Optional[Iterable[Union[str, Path]]] = None) -> ShadowTree:
        """Load a shadow tree; with no explicit paths, every .py under the root
        except VCS/venv/snapshot directories."""
        if paths is None:
            paths = [p for p in self.root.rglob("*.py")
                     if not (_EXCLUDED_DIRS & set(p.relative_to(self.root).parts))]
        return ShadowTree.load(self.root, paths)

    # -- the transaction ------------------------------------------------------

    async def commit(self, ctx: Any, tree: ShadowTree) -> CodemodResult:
        """Verify the shadow, then write it to the host inside `ctx`.

        Failure map -- every path is explicit:
          * pre-verifier raises   -> VerificationFailed, host untouched, no WAL
                                     intent (no side effect means no intent owed)
          * write step fails      -> the saga sees UNKNOWN and compensates via
                                     the durable manifest
          * post-verifier raises  -> VerificationFailed propagates; the
                                     surrounding saga rolls the files back
          * nothing changed       -> applied=False, and nothing is written,
                                     logged, or snapshotted -- a no-op that
                                     reports itself as a no-op
        """
        if tree.root != self.root:
            raise CodemodError(
                f"shadow tree rooted at {tree.root} does not belong to this "
                f"transaction (root={self.root})")

        changed = tree.changed
        if not changed:
            return CodemodResult(applied=False, txn_id=self.txn_id,
                                 files=(), manifest=None)

        for hook in self.verifiers:
            await _run_hook(hook, tree, stage="pre-verify")

        files = tuple(sorted(changed))
        snapdir = self.snapshot_dir / self.txn_id
        manifest_path = snapdir / MANIFEST_NAME

        def _write_all(files: Sequence[str], txn_id: str) -> dict:
            # `files`/`txn_id` arrive as forward_kwargs so the pre-flight gate
            # can see them and the WAL records them on the STEP_INTENT; the
            # actual data flows through the closure.
            _persist_snapshot(self.root, snapdir, manifest_path, changed, txn_id)
            for rel in files:
                m = changed[rel]
                _atomic_write(m.path, m.source.encode(m.encoding))
            return {"manifest": str(manifest_path), "files": list(files)}

        def _compensate(_result: Any) -> Compensation:
            # The manifest path is derived from txn_id alone, so this factory
            # is valid even for an UNKNOWN outcome (_result is None): if the
            # manifest never became durable, the handler proves no file was
            # mutated and restores nothing -- certainty from ordering, not a
            # guess.
            # `fn` is the registered handler itself, invoked with the same JSON
            # kwargs the daemon would use -- one callable, one payload, whether
            # the rollback runs in-process or from saga-recoveryd after a kill.
            return Compensation(
                fn=restore_files,
                handler="codemod.restore_files",
                kwargs={"manifest": str(manifest_path)},
                description=f"restore {len(files)} file(s) from codemod snapshot {self.txn_id}",
            )

        result = await ctx.execute(
            tool="codemod.apply",
            semantics=ActionSemantics.COMPENSABLE,
            forward=_write_all,
            forward_kwargs={"files": list(files), "txn_id": self.txn_id},
            compensate=_compensate,
        )

        for hook in self.post_verifiers:
            await _run_hook(hook, tree, stage="post-verify")

        return CodemodResult(applied=True, txn_id=self.txn_id,
                             files=files, manifest=result["manifest"])


# -- the recovery handler ------------------------------------------------------

@compensator("codemod.restore_files")
def restore_files(manifest: str) -> dict:
    """Restore every file recorded in a codemod snapshot manifest.

    This is the compensation `saga-recoveryd` runs after a SIGKILL, so it obeys
    the recovery rules:

      * **Idempotent.** Restoring the same manifest twice writes the same bytes
        twice. Safe to retry, safe to race (the atomic replace makes the last
        writer win with a complete file either way).
      * **Proof, not hope.** Every snapshot's bytes are re-hashed against the
        sha256 recorded at commit time, and every target path must resolve
        inside the recorded root. A mismatch raises RestoreIntegrityError; it
        never "restores" corrupted or misdirected bytes silently.
      * **Maximal, then honest.** It restores everything it can, and if any
        file failed it raises at the end naming each one -- the daemon then
        records COMPENSATION_FAILED with the full picture, instead of a report
        that stops at the first casualty.
      * An absent manifest is proof of a no-op: the commit path fsyncs the
        manifest *before* mutating any host file, so no manifest means no
        mutation ever happened.

    Note the contract is "restore the pre-transaction bytes": edits made to a
    target file *after* the codemod committed are overwritten by a rollback,
    which is what rolling back means.
    """
    manifest_p = Path(manifest)
    if not manifest_p.exists():
        return {"restored": 0, "deleted": 0,
                "reason": "manifest absent: the snapshot phase never completed, "
                          "so no host file was mutated; nothing to restore"}

    doc = json.loads(manifest_p.read_text("utf-8"))
    root = Path(doc["root"])
    snapdir = manifest_p.parent
    restored = deleted = 0
    failures: List[str] = []

    for entry in doc["entries"]:
        rel = entry["rel"]
        try:
            target = (root / rel).resolve()
            if not target.is_relative_to(root):
                raise RestoreIntegrityError(
                    f"{rel!r} escapes the project root {root}; refusing")
            if not entry["existed"]:
                target.unlink(missing_ok=True)
                deleted += 1
                continue
            snap = (snapdir / entry["snapshot"]).resolve()
            if not snap.is_relative_to(snapdir):
                raise RestoreIntegrityError(
                    f"snapshot path for {rel!r} escapes {snapdir}; refusing")
            blob = snap.read_bytes()
            digest = hashlib.sha256(blob).hexdigest()
            if digest != entry["sha256"]:
                raise RestoreIntegrityError(
                    f"snapshot for {rel!r} does not match its recorded sha256 "
                    f"({digest} != {entry['sha256']}); the snapshot is corrupt "
                    f"or tampered and will not be written back")
            _atomic_write(target, blob)
            restored += 1
        except (OSError, RestoreIntegrityError, KeyError) as exc:
            failures.append(f"{rel}: {exc!r}")

    if failures:
        raise RestoreIntegrityError(
            f"restored {restored} and deleted {deleted} file(s), but "
            f"{len(failures)} could not be restored:\n  " + "\n  ".join(failures))
    return {"restored": restored, "deleted": deleted}


# -- internals -----------------------------------------------------------------

def _prove_compiles(source: str, path: Path) -> ast.Module:
    """Parse AND compile. `ast.parse` alone accepts grammar-valid programs the
    bytecode compiler rejects (module-level nonlocal, some scoping errors), and
    a codemod engine that stages those has shipped a broken file with a green
    check next to it."""
    tree = ast.parse(source, filename=str(path))
    compile(tree, str(path), "exec", dont_inherit=True)
    return tree


async def _run_hook(hook: Verifier, tree: ShadowTree, *, stage: str) -> None:
    name = getattr(hook, "__qualname__", repr(hook))
    try:
        out = hook(tree)
        if hasattr(out, "__await__"):
            await out  # type: ignore[misc]
    except CodemodError:
        raise
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise VerificationFailed(f"{stage} hook {name} vetoed the codemod: {exc!r}") from exc


def _rel_or_raise(path: Path, root: Path) -> str:
    if not path.is_relative_to(root):
        raise CodemodError(f"{path} is outside the transaction root {root}")
    return path.relative_to(root).as_posix()


def _persist_snapshot(root: Path, snapdir: Path, manifest_path: Path,
                      changed: Mapping[str, ShadowModule], txn_id: str) -> None:
    """Make the rollback state durable BEFORE the first mutation. The same
    ordering the WAL enforces for intents, applied to file bytes: nothing may
    change until its undo is on disk and fsynced."""
    entries = []
    for rel in sorted(changed):
        m = changed[rel]
        if m.original_bytes is None:
            entries.append({"rel": rel, "existed": False})
            continue
        snap = snapdir / "files" / rel
        snap.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(snap, m.original_bytes)
        entries.append({
            "rel": rel,
            "existed": True,
            "sha256": hashlib.sha256(m.original_bytes).hexdigest(),
            "snapshot": (Path("files") / rel).as_posix(),
        })
    manifest = {"txn_id": txn_id, "root": str(root), "entries": entries}
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(manifest_path, json.dumps(manifest, indent=2).encode("utf-8"))


def _atomic_write(target: Path, blob: bytes) -> None:
    """tmp file in the same directory + fsync + os.replace. Readers see the old
    complete file or the new complete file, never a torn one. (Directory-entry
    fsync is not available on Windows; per-file durability is.)"""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".agent-saga-tmp")
    with open(tmp, "wb") as fh:
        fh.write(blob)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, target)


__all__ = [
    "AstTransaction",
    "CodemodError",
    "CodemodResult",
    "RestoreIntegrityError",
    "ShadowModule",
    "ShadowRejected",
    "ShadowTree",
    "Transform",
    "VerificationFailed",
    "Verifier",
    "restore_files",
]
