"""Durable-target snapshots.

The in-process `reversible()` in snapshot.py covers state that dies with the
process. This covers the other kind: state that is *private to the saga* but
*survives a crash* -- a config file the agent rewrites, a generated artifact, a
scratch document it owns.

That difference forces three design changes away from the in-process version:

  1. COMPENSABLE, not REVERSIBLE. A crash leaves the file on disk, so the undo
     must be crash-recoverable: a registry-backed handler saga-recoveryd can run,
     not an in-process closure.

  2. The snapshot bytes go to a *store*, and only a reference goes in the WAL. A
     10 MB file's prior contents must never be fsynced into the log. This mirrors
     the credential-reference rule: the WAL carries a pointer, the payload lives
     elsewhere. The daemon resolves the same store, exactly as it resolves the
     same credentials.

  3. A guard. Between the agent's write and the rollback, something outside the
     saga may have edited the file. Restoring blindly would discard that edit, so
     restore verifies the file still holds what the saga wrote and refuses (→
     human) if it does not -- the same stance as the Postgres and Salesforce
     connectors.
"""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, runtime_checkable

from .registry import compensator
from .semantics import ActionSemantics, Compensation

logger = logging.getLogger("agent_saga.durable")


# --------------------------------------------------------------------------
# Snapshot store -- pointer in the WAL, bytes here
# --------------------------------------------------------------------------

@runtime_checkable
class SnapshotStore(Protocol):
    def put(self, snapshot_id: str, blob: bytes) -> None: ...
    def get(self, snapshot_id: str) -> bytes: ...
    def delete(self, snapshot_id: str) -> None: ...


class FileSnapshotStore:
    """Default store: one file per snapshot under a shared directory.

    The directory must be reachable by both the agent and the recovery daemon --
    a local path in single-host deployments, a shared/object-backed mount
    otherwise. Swap in an S3/GCS-backed store via set_snapshot_store for a real
    fleet.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, snapshot_id: str) -> Path:
        # Reject anything that could escape the root; snapshot ids are hex.
        if not snapshot_id or any(c in snapshot_id for c in "/\\.") :
            raise ValueError(f"invalid snapshot id {snapshot_id!r}")
        return self.root / snapshot_id

    def put(self, snapshot_id: str, blob: bytes) -> None:
        path = self._path(snapshot_id)
        # Write-then-rename so a crash never leaves a half-written snapshot that
        # restore would happily apply.
        tmp = path.with_suffix(".tmp")
        with open(tmp, "wb") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def get(self, snapshot_id: str) -> bytes:
        return self._path(snapshot_id).read_bytes()

    def delete(self, snapshot_id: str) -> None:
        try:
            self._path(snapshot_id).unlink()
        except FileNotFoundError:
            pass


_STORE: Optional[SnapshotStore] = None


def set_snapshot_store(store: Optional[SnapshotStore]) -> None:
    global _STORE
    _STORE = store


def get_snapshot_store() -> SnapshotStore:
    if _STORE is not None:
        return _STORE
    root = os.environ.get("AGENT_SAGA_SNAPSHOT_DIR", ".agent_saga_snapshots")
    return FileSnapshotStore(root)


# --------------------------------------------------------------------------
# File target
# --------------------------------------------------------------------------

class StaleFile(RuntimeError):
    """The file changed after the saga wrote it. Restoring would discard whoever
    edited it in the meantime."""


def _sha(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _read_if_exists(path: Path) -> Optional[bytes]:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


@compensator("durable.restore_file")
def restore_file(
    path: str,
    existed: bool,
    snapshot_id: Optional[str],
    guard_sha: Optional[str],
) -> dict:
    """Put a file back the way it was before the saga touched it.

    `existed=False` means the saga created the file, so the undo is to delete it.
    `guard_sha` is the hash of what the saga wrote; if the file no longer matches,
    someone edited it after us and we refuse rather than clobber their change.
    A None guard (an UNKNOWN forward outcome, where we never learned what landed)
    restores unconditionally -- acceptable only because the target is saga-private.
    """
    p = Path(path)
    current = _read_if_exists(p)

    if guard_sha is not None:
        current_sha = _sha(current) if current is not None else None
        if current_sha != guard_sha:
            raise StaleFile(
                f"{path} changed after this saga wrote it "
                f"(sha {current_sha} != {guard_sha}); refusing to restore."
            )

    if not existed:
        if current is not None:
            p.unlink()
        logger.info("restored %s by deleting the saga-created file", path)
        return {"path": path, "status": "deleted"}

    blob = get_snapshot_store().get(snapshot_id)  # type: ignore[arg-type]
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".restore.tmp")
    with open(tmp, "wb") as fh:
        fh.write(blob)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, p)
    logger.info("restored %s from snapshot %s (%d bytes)", path, snapshot_id, len(blob))
    return {"path": path, "status": "restored", "bytes": len(blob)}


async def snapshot_file(
    ctx,
    *,
    path: str | Path,
    mutate: Callable[[str], Any],
    store: Optional[SnapshotStore] = None,
    tool: str = "durable.file",
) -> Any:
    """Let the agent rewrite a file, capturing its prior contents so the change
    is crash-recoverably undoable.

        await snapshot_file(ctx, path="config.yaml",
                            mutate=lambda p: Path(p).write_text(new_yaml))

    On rollback -- in-process or via saga-recoveryd after a crash -- the file is
    restored to its prior bytes, or deleted if the saga created it, provided no
    one edited it in the meantime.
    """
    p = Path(path)
    the_store = store or get_snapshot_store()

    # Capture BEFORE the mutation. Store the prior bytes now; the WAL will carry
    # only the snapshot id.
    prior = _read_if_exists(p)
    existed = prior is not None
    snapshot_id: Optional[str] = None
    if existed:
        snapshot_id = uuid.uuid4().hex
        the_store.put(snapshot_id, prior)  # type: ignore[arg-type]

    def _forward() -> dict:
        result = mutate(str(p))
        # The hash of what we left on disk is the guard the rollback checks.
        after = _read_if_exists(p)
        return {"result": result,
                "guard_sha": _sha(after) if after is not None else None}

    def _compensate(result: Any) -> Compensation:
        # result is None on an UNKNOWN outcome; the prior snapshot is valid
        # regardless, so we restore either way (guard omitted when unknown).
        guard = result["guard_sha"] if result else None
        return Compensation(
            fn=restore_file,
            handler="durable.restore_file",
            kwargs={
                "path": str(p),
                "existed": existed,
                "snapshot_id": snapshot_id,
                "guard_sha": guard,
            },
            description=f"restore file {p}",
            idempotency_key=f"restore-{snapshot_id or p}",
        )

    result = await ctx.execute(
        tool=tool,
        # Durable + private = COMPENSABLE. It survives a crash, so it must be
        # recoverable; classifying it REVERSIBLE would skip the fsync and lose it.
        semantics=ActionSemantics.COMPENSABLE,
        forward=_forward,
        compensate=_compensate,
    )
    return result["result"]


__all__ = [
    "SnapshotStore",
    "FileSnapshotStore",
    "set_snapshot_store",
    "get_snapshot_store",
    "snapshot_file",
    "restore_file",
    "StaleFile",
]
