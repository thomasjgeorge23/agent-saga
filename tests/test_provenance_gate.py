"""Argument provenance: refuse the call when a critical number was invented.

The failure this exists for passes every other control in the package. A
hallucinated `amount=4200` is within budget, COMPENSABLE, refundable, logged,
and provable — every mechanism works perfectly while the wrong amount leaves
the building.

So the tests are mostly about the ways the check must refuse to be fooled:

1. Untagged means MODEL. Absence of provenance is not evidence of provenance.
2. SOURCED is verified against the document, never accepted on the label. A
   receipt that stopped resolving, or a value not present in the span it claims,
   downgrades to MODEL and is refused.
3. A rule on an argument that was not supplied fails rather than silently
   stopping applying.
"""

import pytest
from conftest import aio

from agent_saga.context_broker import ContextBroker
from agent_saga.provenance_gate import (
    Provenance,
    ProvenancePolicy,
    ProvenanceViolation,
    Tagged,
    derived,
    sourced,
    user,
)

INVOICE = "Invoice INV-77\nSubtotal: 3500\nTax: 700\nTotal due: 4200 GBP\n"


@pytest.fixture
def broker():
    b = ContextBroker(prefix="P")
    spans = b.add_document("invoice", INVOICE, chunk_chars=4000)
    b.admit_summary("invoice INV-77 totals 4200 GBP", spans)
    return b


@pytest.fixture
def span(broker):
    return broker.receipts("s1")[0]


# -- 1. untagged is MODEL, and MODEL is refused -----------------------------------

def test_a_bare_value_is_treated_as_model_invented(broker):
    """The default must fail safe: the one argument someone forgot to tag would
    otherwise be exactly the one that sails through."""
    policy = ProvenancePolicy().require("stripe.charge", "amount", Provenance.USER)

    assert policy.classify(4200, broker=broker) is Provenance.MODEL
    with pytest.raises(ProvenanceViolation) as excinfo:
        policy.check("stripe.charge", {"amount": 4200}, broker=broker)
    assert "is MODEL but USER or better is required" in str(excinfo.value)


def test_a_user_supplied_value_passes_a_user_floor(broker):
    policy = ProvenancePolicy().require("stripe.charge", "amount", Provenance.USER)
    policy.check("stripe.charge", {"amount": user(4200)}, broker=broker)


def test_a_sourced_value_passes_a_user_floor(broker, span):
    """The ordering is the point: SOURCED is stronger than USER."""
    policy = ProvenancePolicy().require("stripe.charge", "amount", Provenance.USER)
    policy.check("stripe.charge", {"amount": sourced(4200, span)}, broker=broker)


def test_every_failing_argument_is_reported_at_once(broker):
    policy = (ProvenancePolicy()
              .require("email.send", "to", Provenance.SOURCED)
              .require("email.send", "subject", Provenance.USER))

    with pytest.raises(ProvenanceViolation) as excinfo:
        policy.check("email.send", {"to": "a@b.com", "subject": "hi"}, broker=broker)
    assert len(excinfo.value.failures) == 2


# -- 2. SOURCED is verified, not accepted ---------------------------------------------

def test_a_sourced_claim_is_checked_against_the_document(broker, span):
    policy = ProvenancePolicy().require("stripe.charge", "amount", Provenance.SOURCED)

    # 4200 really is in the invoice
    policy.check("stripe.charge", {"amount": sourced(4200, span)}, broker=broker)

    # 9900 is not, however it is labelled
    with pytest.raises(ProvenanceViolation) as excinfo:
        policy.check("stripe.charge", {"amount": sourced(9900, span)}, broker=broker)
    assert "the receipt did not check out" in str(excinfo.value)


def test_a_receipt_that_stopped_resolving_downgrades_to_model(broker, span):
    """The document drifted, so the receipt proves nothing any more. The value
    must stop counting as sourced the moment that is true."""
    policy = ProvenancePolicy().require("stripe.charge", "amount", Provenance.SOURCED)
    broker.cold.put("invoice", INVOICE.replace("4200", "9999"))

    assert policy.classify(sourced(4200, span), broker=broker) is Provenance.MODEL
    with pytest.raises(ProvenanceViolation):
        policy.check("stripe.charge", {"amount": sourced(4200, span)}, broker=broker)


def test_a_sourced_claim_with_no_broker_cannot_be_verified_so_is_refused(span):
    """Taking the label on trust when there is nothing to check it against is
    exactly the hole this module exists to close."""
    policy = ProvenancePolicy().require("stripe.charge", "amount", Provenance.SOURCED)
    assert policy.classify(sourced(4200, span), broker=None) is Provenance.MODEL
    with pytest.raises(ProvenanceViolation):
        policy.check("stripe.charge", {"amount": sourced(4200, span)}, broker=None)


def test_sourced_requires_a_receipt_at_construction():
    with pytest.raises(ValueError, match="requires a receipt"):
        sourced(4200, None)


def test_derived_requires_a_stated_derivation():
    """DERIVED without a derivation is MODEL with better manners."""
    with pytest.raises(ValueError, match="requires a note"):
        derived(4200, "  ")
    assert derived(4200, "subtotal 3500 + tax 700").provenance is Provenance.DERIVED


def test_a_derived_value_does_not_satisfy_a_user_floor(broker):
    policy = ProvenancePolicy().require("stripe.charge", "amount", Provenance.USER)
    with pytest.raises(ProvenanceViolation):
        policy.check("stripe.charge",
                     {"amount": derived(4200, "3500 + 700")}, broker=broker)


# -- 3. a rule cannot silently stop applying --------------------------------------------

def test_a_rule_on_a_missing_argument_fails_rather_than_passing(broker):
    policy = ProvenancePolicy().require("stripe.charge", "amount", Provenance.USER)
    with pytest.raises(ProvenanceViolation) as excinfo:
        policy.check("stripe.charge", {"currency": "GBP"}, broker=broker)
    assert "was not supplied" in str(excinfo.value)


def test_requiring_model_is_refused_as_a_rule():
    """A floor everything meets reads as a control while enforcing nothing."""
    with pytest.raises(ValueError, match="floor everything already meets"):
        ProvenancePolicy().require("t", "a", Provenance.MODEL)


def test_rules_apply_only_to_their_own_tool(broker):
    policy = ProvenancePolicy().require("stripe.charge", "amount", Provenance.USER)
    policy.check("search.query", {"amount": 4200}, broker=broker)      # unaffected


# -- prohibitions -------------------------------------------------------------------------

def test_a_prohibited_tool_is_refused_whatever_its_arguments(broker):
    policy = ProvenancePolicy().prohibit(
        "wire.transfer", "this deployment never initiates wire transfers")

    with pytest.raises(ProvenanceViolation) as excinfo:
        policy.check("wire.transfer", {"amount": user(1)}, broker=broker)
    assert "never initiates wire transfers" in str(excinfo.value)


def test_a_prohibition_needs_a_reason():
    with pytest.raises(ValueError, match="needs a reason"):
        ProvenancePolicy().prohibit("wire.transfer", "")


# -- plumbing ------------------------------------------------------------------------------

def test_unwrap_hands_plain_values_to_the_real_tool():
    kwargs = {"amount": user(4200), "currency": "GBP"}
    assert ProvenancePolicy.unwrap(kwargs) == {"amount": 4200, "currency": "GBP"}


def test_the_policy_is_readable_and_machine_readable():
    policy = (ProvenancePolicy()
              .require("stripe.charge", "amount", Provenance.USER)
              .prohibit("wire.transfer", "not in scope"))

    text = policy.format_text()
    assert "requires USER or better" in text
    assert "PROHIBITED: not in scope" in text

    data = policy.describe()
    assert data["requirements"][0]["minimum"] == "USER"
    assert data["prohibited"][0]["tool"] == "wire.transfer"


def test_an_empty_policy_says_it_enforces_nothing():
    assert "enforces nothing" in ProvenancePolicy().format_text()


def test_it_adapts_into_a_gate_rule(broker):
    """Enforced on the same path as budgets and approvals, not as a second
    thing everyone remembers separately."""
    from types import SimpleNamespace

    policy = ProvenancePolicy().require("stripe.charge", "amount", Provenance.USER)
    rule = policy.as_gate_rule(broker=broker)

    rule(SimpleNamespace(tool="stripe.charge", kwargs={"amount": user(4200)}))
    with pytest.raises(ProvenanceViolation):
        rule(SimpleNamespace(tool="stripe.charge", kwargs={"amount": 4200}))


# -- the scenario the module exists for -----------------------------------------------------

def test_the_hallucinated_amount_is_refused_before_it_can_be_charged(broker, span):
    """Every other control passes this call. This one does not."""
    policy = ProvenancePolicy().require("stripe.charge", "amount", Provenance.SOURCED)

    # what the model should do: cite the invoice it read
    policy.check("stripe.charge", {"amount": sourced(4200, span)}, broker=broker)

    # what a hallucinating model does: produce a plausible number from nowhere
    with pytest.raises(ProvenanceViolation) as excinfo:
        policy.check("stripe.charge", {"amount": 4300}, broker=broker)
    assert "MODEL" in str(excinfo.value)
