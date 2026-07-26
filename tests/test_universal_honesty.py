"""The Universal engine must not overstate what it did.

Three defects motivated these tests, all of the same family -- a safety layer
reporting success it did not achieve:

1. `patch_all()` returned {"openai": True, ...} while patching nothing.
2. `audit_hash` was labelled `sha256_` but used Python's per-process-randomised
   builtin hash(), so the same input signed differently on every run.
3. `enhance()` raised inside a running event loop -- exactly where agents live.

False assurance in a safety library is worse than no feature, because the user
stops looking for the real protection.
"""

import asyncio

import pytest
from conftest import aio

from agent_saga.universal import (
    _audit_signature,
    aenhance,
    enhance,
    patch_all,
    unpatch_all,
)


@pytest.fixture(autouse=True)
def _restore_sdks():
    yield
    unpatch_all()


# -- patch_all tells the truth -------------------------------------------------

def test_patch_all_never_claims_an_sdk_it_did_not_patch():
    report = patch_all()
    for provider, state in report.items():
        assert state == "patched" or state == "not_installed" \
            or state == "already_patched" or state.startswith("failed:"), \
            f"{provider} reported an unrecognised state: {state}"
        # the original bug: claiming success for an SDK that isn't even importable
        if state == "patched":
            __import__(provider if provider != "gemini" else "google.generativeai")


def test_patch_all_reports_uninstalled_sdks_honestly():
    """In a bare environment no LLM SDK is installed, so nothing may claim
    'patched'. This is the exact regression: True for all four, always."""
    report = patch_all()
    assert report, "patch_all must report per-SDK, not an empty dict"
    for provider, state in report.items():
        if state == "patched":
            continue                      # genuinely installed here
        assert state != True             # noqa: E712 - the old bug was literal True
        assert isinstance(state, str)


def test_patch_all_is_reversible():
    first = patch_all()
    second = patch_all()
    for provider, state in second.items():
        if first.get(provider) == "patched":
            assert state == "already_patched"   # never double-wraps
    restored = unpatch_all()
    assert set(restored) <= set(first)
    for provider in restored:
        assert restored[provider] == "restored"


def test_patching_a_real_module_wraps_and_restores(monkeypatch):
    """Prove the machinery genuinely swaps a callable -- using a stand-in module
    so the test does not require a vendor SDK."""
    import sys
    import types

    from agent_saga import universal

    mod = types.ModuleType("fake_llm_sdk")
    calls = []

    def create(prompt):
        calls.append(prompt)
        return {"text": "ok"}

    mod.create = create
    monkeypatch.setitem(sys.modules, "fake_llm_sdk", mod)
    monkeypatch.setitem(universal._PATCH_TARGETS, "fake", ("fake_llm_sdk", "create"))

    seen = []
    report = patch_all(record=lambda provider, result: seen.append((provider, result)))
    assert report["fake"] == "patched"
    assert getattr(mod.create, "__agent_saga_patched__", False)

    assert mod.create("hi") == {"text": "ok"}      # response passes through unaltered
    assert calls == ["hi"]                          # the real call still happened
    assert seen == [("fake", {"text": "ok"})]       # and we observed it

    unpatch_all()
    assert mod.create is create                     # exactly restored


def test_a_failing_record_hook_never_breaks_the_users_llm_call(monkeypatch):
    import sys
    import types

    from agent_saga import universal

    mod = types.ModuleType("fake_llm_sdk2")
    mod.create = lambda prompt: {"text": "fine"}
    monkeypatch.setitem(sys.modules, "fake_llm_sdk2", mod)
    monkeypatch.setitem(universal._PATCH_TARGETS, "fake2", ("fake_llm_sdk2", "create"))

    def boom(provider, result):
        raise RuntimeError("bookkeeping exploded")

    patch_all(record=boom)
    assert mod.create("hi") == {"text": "fine"}     # our failure must not surface
    unpatch_all()


# -- the audit signature is real and reproducible ------------------------------

def test_audit_signature_is_real_sha256_and_reproducible():
    a = _audit_signature("hello", ["t1", "t2"])
    b = _audit_signature("hello", ["t1", "t2"])
    assert a == b                                    # stable within a process
    assert a.startswith("sha256:")
    assert len(a) == len("sha256:") + 64             # a real 256-bit digest

    import hashlib
    import json
    payload = json.dumps({"content": "hello", "executed_steps": ["t1", "t2"]},
                         sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                         default=str)
    assert a == "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def test_audit_signature_is_reproducible_across_processes():
    """The original bug: builtin hash() is salted per process, so an auditor in
    another process could never recompute the signature."""
    import subprocess
    import sys

    code = ("from agent_saga.universal import _audit_signature;"
            "print(_audit_signature('hello', ['t1','t2']))")
    outs = {
        subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, check=True).stdout.strip()
        for _ in range(2)
    }
    assert len(outs) == 1                            # identical across processes
    assert outs.pop() == _audit_signature("hello", ["t1", "t2"])


def test_different_steps_sign_differently():
    assert _audit_signature("x", ["a"]) != _audit_signature("x", ["b"])
    assert _audit_signature("x", ["a"]) != _audit_signature("y", ["a"])


# -- enhance() works where agents actually live --------------------------------

@aio
async def test_aenhance_works_inside_a_running_loop():
    result = await aenhance("the model said something")
    assert result.original_content == "the model said something"
    assert result.audit_hash.startswith("sha256:")


@aio
async def test_sync_enhance_refuses_clearly_inside_a_loop():
    """It cannot block a running loop -- so it must say so, not raise an opaque
    'event loop is already running' from two frames deep."""
    with pytest.raises(RuntimeError, match="aenhance"):
        enhance("x")


def test_sync_enhance_still_works_outside_a_loop():
    result = enhance("plain script context")
    assert result.original_content == "plain script context"
    assert result.status == "SUCCESS"
