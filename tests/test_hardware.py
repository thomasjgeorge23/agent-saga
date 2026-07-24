"""Hardware-bound approval (Phase 1.2): an effectful step requires a signature
from a key the model cannot reach, bound to the exact action."""

import pytest
from conftest import aio

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives import serialization as ser
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_saga.gate import GateContext, PreFlightGate, Verdict, Rule
from agent_saga.hardware import (
    ActionChallenge, HardwareApprovalProvider, action_digest)
from agent_saga.semantics import ActionSemantics as S


def _key():
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)
    return sk, pk


def _ctx(tool, semantics=S.IRREVERSIBLE, **kw):
    return GateContext(tool=tool, semantics=semantics, kwargs=kw)


def _provider(**kw):
    sk, pk = _key()
    return sk, HardwareApprovalProvider({"passkey-1": pk}, **kw)


def _sign(sk, provider, ctx):
    ch = provider.challenge_for(ctx)
    return ch, sk.sign(ch.signing_payload())


# -- the attack this exists to stop -------------------------------------------

def test_signature_for_one_action_cannot_authorise_another():
    sk, p = _provider()
    benign = _ctx("balance.check", account="acct-1")
    wire = _ctx("wire.transfer", amount=1_000_000, to="acct-4471")

    ch, sig = _sign(sk, p, benign)              # human sees + signs "balance.check"
    assert p.submit(ch.challenge_id, "passkey-1", sig) is True
    assert p.approved(benign) is True
    assert p.approved(wire) is False            # the injected wire is NOT covered


def test_mutating_arguments_after_signing_invalidates_approval():
    sk, p = _provider()
    real = _ctx("wire.transfer", amount=1_000_000, to="acct-1")
    ch, sig = _sign(sk, p, real)
    p.submit(ch.challenge_id, "passkey-1", sig)

    mutated = _ctx("wire.transfer", amount=9_999_999, to="acct-1")
    assert p.approved(real) is True
    assert p.approved(mutated) is False         # one digit changed -> no authority


def test_action_digest_covers_tool_semantics_and_kwargs():
    a = action_digest(_ctx("t", amount=1))
    assert a != action_digest(_ctx("t", amount=2))          # kwargs
    assert a != action_digest(_ctx("other", amount=1))      # tool
    assert a != action_digest(_ctx("t", S.COMPENSABLE, amount=1))  # semantics


# -- assertion validation -----------------------------------------------------

def test_replayed_signature_is_refused():
    sk, p = _provider()
    ctx = _ctx("wire.transfer", amount=1)
    ch, sig = _sign(sk, p, ctx)
    assert p.submit(ch.challenge_id, "passkey-1", sig) is True
    assert p.submit(ch.challenge_id, "passkey-1", sig) is False


def test_signature_from_an_unregistered_key_is_refused():
    sk, p = _provider()
    attacker = Ed25519PrivateKey.generate()
    ctx = _ctx("wire.transfer", amount=1)
    ch = p.challenge_for(ctx)
    assert p.submit(ch.challenge_id, "passkey-1", attacker.sign(ch.signing_payload())) is False
    assert p.approved(ctx) is False


def test_unknown_credential_is_refused():
    sk, p = _provider()
    ctx = _ctx("wire.transfer", amount=1)
    ch, sig = _sign(sk, p, ctx)
    assert p.submit(ch.challenge_id, "nobody", sig) is False


def test_expired_challenge_is_refused():
    clock = {"t": 1000.0}
    sk, p = _provider(challenge_ttl=60, clock=lambda: clock["t"])
    ctx = _ctx("wire.transfer", amount=1)
    ch, sig = _sign(sk, p, ctx)
    clock["t"] += 61
    assert p.submit(ch.challenge_id, "passkey-1", sig) is False


def test_unknown_challenge_is_refused():
    sk, p = _provider()
    ctx = _ctx("wire.transfer", amount=1)
    ch, sig = _sign(sk, p, ctx)
    assert p.submit("not-a-challenge", "passkey-1", sig) is False


# -- gate integration ---------------------------------------------------------

@aio
async def test_reversible_step_needs_no_hardware():
    sk, p = _provider()
    assert await p.decide(_ctx("db.read", S.REVERSIBLE)) is True


@aio
async def test_decide_grants_once_signed_and_is_single_use():
    sk, p = _provider()
    ctx = _ctx("wire.transfer", amount=500)
    ch, sig = _sign(sk, p, ctx)
    p.submit(ch.challenge_id, "passkey-1", sig)

    assert await p.decide(ctx) is True          # consumes the approval
    assert p.approved(ctx) is False             # cannot be reused for a 2nd wire


@aio
async def test_decide_refuses_without_a_signature():
    sk, p = _provider(timeout=0.05, poll_interval=0.01)
    assert await p.decide(_ctx("wire.transfer", amount=1)) is False


@aio
async def test_gate_blocks_an_effectful_step_with_no_hardware_approval():
    sk, p = _provider(timeout=0.05, poll_interval=0.01)
    gate = PreFlightGate(
        rules=[Rule(name="needs-hw", when=lambda c: True,
                    verdict=Verdict.REQUIRE_APPROVAL, reason="hardware required")],
        approval_provider=p)
    from agent_saga.gate import PreFlightViolation
    with pytest.raises(PreFlightViolation):
        await gate.evaluate(_ctx("wire.transfer", amount=1_000_000))


def test_challenge_payload_binds_nonce_and_digest():
    sk, p = _provider()
    ctx = _ctx("wire.transfer", amount=1)
    ch = p.challenge_for(ctx)
    payload = ch.signing_payload().decode()
    assert ch.challenge in payload and ch.action_digest in payload
    # what goes to the browser carries no secret
    client = ch.to_client()
    assert set(client) >= {"challenge", "action_digest", "tool"}


def test_on_challenge_callback_fires():
    seen = []
    sk, p = _provider(on_challenge=seen.append)
    p.challenge_for(_ctx("wire.transfer", amount=1))
    assert len(seen) == 1 and isinstance(seen[0], ActionChallenge)
