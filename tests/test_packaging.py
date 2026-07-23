"""Packaging contract: optional extras and the dependency-free core guarantee.

These tests read pyproject.toml directly so a bad edit to the extras table fails
CI instead of a user's `pip install`. They also assert the core installs and
imports with zero third-party packages, and that every heavy backend fails with
an actionable "install the extra" hint rather than a raw ModuleNotFoundError.
"""

import tomllib
from pathlib import Path

import pytest

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _extras():
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    return data["project"]["optional-dependencies"], data["project"]


def test_core_has_no_runtime_dependencies():
    _, project = _extras()
    assert project["dependencies"] == [], "the core engine must stay dependency-free"


def test_required_extras_exist():
    extras, _ = _extras()
    for name in ("redis", "postgres", "otel", "slack", "all"):
        assert name in extras, f"missing optional extra: {name}"


def test_postgres_extra_covers_its_stores_and_adapter():
    extras, _ = _extras()
    joined = " ".join(extras["postgres"])
    assert "asyncpg" in joined, "PostgresWAL/PostgresApprovalStore need asyncpg"
    assert "sqlalchemy" in joined, "SQLAlchemyAdapter needs sqlalchemy"


def test_redis_and_otel_extras():
    extras, _ = _extras()
    assert any("redis" in p for p in extras["redis"])
    assert any("opentelemetry" in p for p in extras["otel"])


def test_slack_extra_is_stdlib_only():
    extras, _ = _extras()
    # SlackBlockKitApp + webhook notifiers are pure stdlib; the extra exists as a
    # stable, self-documenting install target that pulls nothing third-party.
    assert extras["slack"] == []


def test_all_is_a_superset_of_every_concrete_extra():
    extras, _ = _extras()
    concrete = set()
    for name, reqs in extras.items():
        if name in ("all", "dev"):
            continue
        concrete |= set(reqs)
    missing = concrete - set(extras["all"])
    assert not missing, f"[all] is missing: {sorted(missing)}"


def test_version_is_dynamic_single_source():
    _, project = _extras()
    assert "version" in project.get("dynamic", []), "version must be dynamic"
    from agent_saga._version import __version__
    assert __version__


def test_core_imports_without_extras():
    # If any heavy dep were imported at module load, this would fail in the
    # dependency-free CI environment (sqlalchemy/redis/asyncpg are not installed).
    import agent_saga  # noqa: F401
    assert agent_saga.__version__


def test_sqlalchemy_adapter_gives_actionable_hint_without_extra():
    try:
        import sqlalchemy  # noqa: F401
        pytest.skip("sqlalchemy is installed; the missing-extra path can't be exercised")
    except ImportError:
        pass
    from agent_saga.adapters.sqlalchemy import SQLAlchemyAdapter
    with pytest.raises(ImportError) as exc:
        SQLAlchemyAdapter(lambda: None)
    assert "agent-saga[postgres]" in str(exc.value)
