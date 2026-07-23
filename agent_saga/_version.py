"""Single source of truth for the package version.

Imported by ``agent_saga/__init__.py`` (for ``agent_saga.__version__``), by the
CLI (``agent-saga --version``), by the dashboard, and read at build time by
Hatchling via ``[tool.hatch.version]`` in ``pyproject.toml``. Keeping the string
in exactly one place means a release bump can never leave the wheel metadata,
the runtime attribute, and the CLI banner disagreeing.

This module deliberately imports nothing from the rest of the package, so it is
cheap to import from the CLI without pulling in the whole dependency graph.
"""

from __future__ import annotations

__version__ = "0.2.2"

__all__ = ["__version__"]
