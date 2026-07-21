"""Recovery lock interface: local default, injectable backend."""

import tempfile
from pathlib import Path

import pytest

from agent_saga import FileLock, InProcessLock, RecoveryDaemon
from agent_saga.locks import RecoveryLock


def test_file_lock_is_mutually_exclusive():
    with tempfile.TemporaryDirectory() as d:
        a = FileLock(Path(d) / "claims")
        b = FileLock(Path(d) / "claims")
        assert a.acquire("saga-1") is True
        assert b.acquire("saga-1") is False   # a holds it
        a.release("saga-1")
        assert b.acquire("saga-1") is True     # now free


def test_file_lock_release_is_idempotent():
    with tempfile.TemporaryDirectory() as d:
        a = FileLock(Path(d) / "claims")
        a.release("never-held")   # no error
        a.acquire("x")
        a.release("x")
        a.release("x")            # again, no error


def test_in_process_lock_excludes_within_the_process():
    lock = InProcessLock()
    assert lock.acquire("k") is True
    assert lock.acquire("k") is False
    lock.release("k")
    assert lock.acquire("k") is True


def test_daemon_defaults_to_a_file_lock():
    with tempfile.TemporaryDirectory() as d:
        daemon = RecoveryDaemon(Path(d) / "wal.jsonl")
        assert isinstance(daemon.lock, FileLock)
        assert isinstance(daemon.lock, RecoveryLock)


def test_daemon_accepts_an_injected_lock():
    """A Redis/DB lock is injected the same way -- the daemon never imports it."""
    calls = []

    class RecordingLock:
        def acquire(self, key):
            calls.append(("acquire", key))
            return True
        def release(self, key):
            calls.append(("release", key))

    with tempfile.TemporaryDirectory() as d:
        daemon = RecoveryDaemon(Path(d) / "wal.jsonl", lock=RecordingLock())
        assert daemon._claim("s1") is True
        daemon._release("s1")
        assert calls == [("acquire", "s1"), ("release", "s1")]


def test_injected_lock_must_satisfy_the_protocol():
    class NotALock:
        pass

    assert not isinstance(NotALock(), RecoveryLock)
    assert isinstance(InProcessLock(), RecoveryLock)
