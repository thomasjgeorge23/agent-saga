"""docs/CAPABILITIES.md must not drift from the code it documents.

A hand-written summary of this package recently invented an API that does not
exist (`crew_tool`, `llama_tool` -- the real name is `wrap_tool` everywhere)
and repeated two claims that had been retracted for cause. Documentation that
overstates a safety library is the same defect as a safety library that
overstates itself; it just fails somewhere else.

So the reference is checked against the source: every CLI command it names must
exist and every command that exists must be named, every connector handler must
really be registered, and every adapter function must really be importable.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "CAPABILITIES.md"

_EXTRACT = r"""
import argparse, importlib, json, pkgutil

from agent_saga.cli import build_parser
parser = build_parser()
sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))

def scan(pkgname):
    pkg = importlib.import_module(pkgname)
    out = {}
    for m in pkgutil.iter_modules(pkg.__path__):
        if m.name.startswith("_"):
            continue
        mod = importlib.import_module(pkgname + "." + m.name)
        out[m.name] = {
            "handlers": sorted({getattr(v, "__compensator_name__")
                                for v in vars(mod).values()
                                if callable(v) and hasattr(v, "__compensator_name__")}),
            "public": sorted(n for n, v in vars(mod).items()
                             if callable(v) and not n.startswith("_")
                             and getattr(v, "__module__", "") == mod.__name__),
        }
    return out

print(json.dumps({
    "cli": sorted(sub.choices),
    "connectors": scan("agent_saga.connectors"),
    "adapters": scan("agent_saga.adapters"),
}))
"""


@pytest.fixture(scope="module")
def text() -> str:
    assert DOC.exists(), f"{DOC} is missing"
    return DOC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def api() -> dict:
    """Introspect the package in a SUBPROCESS.

    Importing every connector registers its `@compensator` handlers into a
    process-global table, and `test_certify` legitimately asserts that
    `stripe.refund` is absent from it. Doing this inline made a documentation
    test change the outcome of a safety test, so the introspection is kept out
    of this process entirely rather than weakening either one.
    """
    result = subprocess.run([sys.executable, "-c", _EXTRACT], cwd=str(ROOT),
                            capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def commands(api) -> set:
    return set(api["cli"])


@pytest.fixture(scope="module")
def handlers(api) -> dict:
    return {name: set(info["handlers"]) for name, info in api["connectors"].items()
            if info["handlers"]}


@pytest.fixture(scope="module")
def adapters(api) -> dict:
    return {name: set(info["public"]) for name, info in api["adapters"].items()}


def _documented_commands(text: str) -> set:
    return set(re.findall(r"`agent-saga ([a-z-]+)", text))


# -- CLI ------------------------------------------------------------------------

def test_every_cli_command_is_documented(text, commands):
    missing = commands - _documented_commands(text)
    assert not missing, (
        f"these commands exist but are undocumented: {sorted(missing)}. A "
        f"capability nobody can find is, for most users, one that does not ship.")


def test_no_invented_cli_commands(text, commands):
    invented = _documented_commands(text) - commands
    assert not invented, f"documented but nonexistent: {sorted(invented)}"


def test_the_documented_command_count_is_right(text, commands):
    stated = int(re.search(r"all (\d+) commands", text).group(1))
    assert stated == len(commands), (
        f"the doc says {stated} commands; the CLI has {len(commands)}")


# -- connectors -------------------------------------------------------------------

def test_every_documented_handler_is_really_registered(text, handlers):
    """A handler named in the docs but absent from the registry would send an
    operator to a compensation the recovery daemon cannot resolve."""
    real = {h for names in handlers.values() for h in names}
    documented = set(re.findall(
        r"`((?:stripe|postgres|salesforce|github|messaging|cloud)\.[a-z_]+)`", text))
    assert documented, "no connector handlers are documented at all"
    invented = documented - real
    assert not invented, f"documented but not registered: {sorted(invented)}"


def test_every_connector_module_is_documented(text, handlers):
    for module in handlers:
        assert f"connectors.{module}" in text, f"connector {module} is undocumented"


def test_the_documented_handler_count_is_right(text, handlers):
    stated = int(re.search(r"(\d+) registered handlers", text).group(1))
    actual = sum(len(names) for names in handlers.values())
    assert stated == actual, f"doc says {stated} handlers; found {actual}"


# -- adapters -----------------------------------------------------------------------

def _adapter_section(text: str) -> str:
    return text.split("## Framework adapters")[1].split("## Connectors")[0]


def test_every_documented_adapter_function_exists(text, adapters):
    """The specific failure this guards: `crew_tool` and `llama_tool` were
    documented elsewhere and have never existed."""
    section = _adapter_section(text)
    checked = 0
    for module, names in adapters.items():
        row = next((line for line in section.splitlines()
                    if f"adapters.{module}`" in line), None)
        if row is None:
            continue
        for claimed in re.findall(r"`([a-zA-Z_]\w+)`", row):
            if claimed.startswith("adapters."):
                continue
            checked += 1
            assert claimed in names, (
                f"docs claim adapters.{module}.{claimed}, which does not exist. "
                f"Real API: {sorted(names)}")
    assert checked > 10, "the adapter table stopped being checked"


def test_wrap_tool_is_the_documented_name_everywhere(text, adapters):
    """The invented names may appear in the prose explaining that they were
    invented -- that sentence is the point of the section. What must never
    happen is them appearing in the table as though they were API."""
    rows = "\n".join(line for line in _adapter_section(text).splitlines()
                     if line.startswith("|"))
    assert "crew_tool" not in rows and "llama_tool" not in rows

    for module in ("langgraph", "crewai", "autogen", "llamaindex", "openai_agents"):
        assert "wrap_tool" in adapters[module], f"{module} lost wrap_tool"


def test_the_documented_adapter_count_is_right(text, adapters):
    stated = int(re.search(r"Framework adapters — (\d+) modules", text).group(1))
    assert stated == len(adapters)


# -- the retractions stay retracted ---------------------------------------------------

def test_the_doc_does_not_re_advertise_retracted_claims(text):
    """v0.5.1 retracted 'two-phase commit' and 'zero-knowledge' for cause. A
    capability reference that reintroduced either would re-advertise a fixed
    vulnerability."""
    body = text.lower()
    assert "is not two-phase commit" in body
    assert "is not zero-knowledge" in body
    assert not re.search(r"(?<!not )\b2pc\b", body)


def test_zero_dependency_claim_is_true(text):
    import tomllib

    assert "Zero required dependencies" in text
    with open(ROOT / "pyproject.toml", "rb") as fh:
        data = tomllib.load(fh)
    assert data["project"]["dependencies"] == []


def test_every_documented_extra_exists(text):
    import tomllib

    with open(ROOT / "pyproject.toml", "rb") as fh:
        data = tomllib.load(fh)
    real = set(data["project"]["optional-dependencies"])
    documented = set(re.findall(r"agent-saga\[([a-z-]+)\]", text))
    assert documented, "no extras are documented at all"
    invented = documented - real
    assert not invented, f"documented extras that do not exist: {sorted(invented)}"
