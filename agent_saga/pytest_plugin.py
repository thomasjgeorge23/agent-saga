"""pytest plugin for testing sagas: fresh WALs, chaos injection, and a
determinism gate.

Enabled automatically once ``agent-saga`` is installed (registered as a
``pytest11`` entry point), or explicitly with
``pytest_plugins = ("agent_saga.pytest_plugin",)`` in a conftest.

Fixtures
--------
``saga_wal``
    A fresh, file-backed WAL unique to each test (so ``.records()`` is
    inspectable). It is handed over *unstarted* -- start it inside your own
    event loop: ``await saga_wal.start()`` -- and best-effort closed on teardown.

``chaos_runner``
    A :class:`~agent_saga.testing.ChaosRunner`, configured from a
    ``@pytest.mark.saga_chaos(fail_after=..., fail_at=[...])`` marker when present.

``assert_saga_deterministic``
    Call ``assert_saga_deterministic(records)`` to fail the test if the event
    stream would not replay deterministically (via ReplayVerifier).
"""

from __future__ import annotations

from typing import Any

import pytest


def pytest_configure(config: Any) -> None:
    config.addinivalue_line(
        "markers",
        "saga_chaos(fail_after=1, fail_at=[...]): configure the chaos_runner "
        "fixture to inject failure at the given saga step(s).",
    )


@pytest.fixture
def saga_wal(tmp_path):
    """A fresh file-backed WAL per test. Unstarted; call `await saga_wal.start()`."""
    from agent_saga.wal.file_wal import FileWAL

    wal = FileWAL(tmp_path / "saga-test.wal")
    yield wal
    # Release the OS file handle and worker thread synchronously. The test's own
    # event loop is gone, so we cannot await close(); closing the handle and
    # shutting the pool directly is enough to avoid a leaked-descriptor
    # ResourceWarning, and teardown must never fail a passing test.
    try:
        if getattr(wal, "_fh", None) is not None:
            wal._fh.close()
            wal._fh = None
    except Exception:
        pass
    try:
        if getattr(wal, "_flush_pool", None) is not None:
            wal._flush_pool.shutdown(wait=False)
            wal._flush_pool = None
    except Exception:
        pass


@pytest.fixture
def chaos_runner(request):
    """A ChaosRunner, configured from a `saga_chaos` marker if the test has one."""
    from agent_saga.testing import ChaosRunner

    marker = request.node.get_closest_marker("saga_chaos")
    if marker is not None:
        return ChaosRunner(*marker.args, **marker.kwargs)
    return ChaosRunner()


@pytest.fixture
def assert_saga_deterministic():
    """Return a callable that fails the test if `records` would not replay
    deterministically."""
    from agent_saga.testing import verify_saga_replay

    def _assert(records: list[dict]) -> None:
        result = verify_saga_replay(list(records))
        if not result.deterministic:
            pytest.fail(
                "saga replay is non-deterministic: "
                + "; ".join(result.mismatches or ["hash mismatch"])
            )

    return _assert
