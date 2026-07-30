"""Failure prediction from the log -- the one statistical control, fenced in.

Everything else in this package is deterministic, so the tests here are mostly
about keeping this module inside its lane:

1. It reuses `corpus` labels, so a step undone for a LATER step's failure is not
   counted as a failure of its own. Counting it would teach the model that
   charging money is dangerous -- true, and useless.
2. It reports lift over the base rate, not a raw frequency. "9 of 12 failed" is
   meaningless when three quarters of everything fails.
3. Below its support threshold it declines to produce a number, and says that
   silence is not a clean bill of health.
4. It ships no automatic blocking gate. A correlation is grounds to look.
"""

import pytest

from agent_saga.risk import FailureModel, RiskAssessment, require_review_above


def saga(saga_id, *steps, aborted=False, fail_last=False):
    """Build WAL records for one saga. `steps` are (tool, kwargs) pairs."""
    out = [{"saga_id": saga_id, "event": "SAGA_START", "name": "t"}]
    for index, (tool, kwargs) in enumerate(steps):
        step_id = f"{saga_id}-{index}"
        out.append({"saga_id": saga_id, "event": "STEP_INTENT", "step_id": step_id,
                    "tool": tool, "semantics": "COMPENSABLE", "kwargs": kwargs})
        is_last = index == len(steps) - 1
        if fail_last and is_last:
            continue                      # intent written, never committed -> REJECTED
        out.append({"saga_id": saga_id, "event": "STEP_COMMITTED",
                    "step_id": step_id, "tool": tool})
        if aborted:
            out.append({"saga_id": saga_id, "event": "COMPENSATED",
                        "step_id": step_id, "tool": tool})
    out.append({"saga_id": saga_id,
                "event": "SAGA_ABORTED" if (aborted or fail_last) else "SAGA_COMPLETE"})
    return out


# -- 1. attribution: collateral is not failure -----------------------------------------

def test_a_step_undone_for_a_later_failure_is_not_counted_as_a_failure():
    """The correctness heart. Without this the model learns that the reliable
    step preceding an unreliable one is itself risky."""
    records = []
    # 8 sagas where charge committed and was rolled back because ship failed
    for i in range(8):
        records += saga(f"s{i}", ("stripe.charge", {"amount": 100}),
                        ("ship.order", {"sku": "x"}), fail_last=True)

    model = FailureModel.fit(records, min_support=3)

    # Only the 8 ship.order steps are trainable. The 8 charges were COLLATERAL
    # -- correct actions undone for someone else's failure -- and are excluded
    # from both numerator and denominator.
    assert model.total == 8
    assert model.failures == 8

    charge = model.assess("stripe.charge", {"amount": 100})
    assert not charge.sufficient_evidence, "collateral steps leaked into the model"

    ship = model.assess("ship.order", {"sku": "x"})
    assert ship.sufficient_evidence
    # With no successes anywhere, base_rate is 1.0 and lift is 1.0 by
    # definition. The module reports that rather than inflating "8/8 failed"
    # into a 100% risk score -- lift cannot inform where there is no variance.
    assert ship.max_lift == pytest.approx(1.0)


def test_only_rejected_examples_count_as_failures():
    records = []
    for i in range(6):
        records += saga(f"ok{i}", ("email.send", {"to": "a@b.com"}))
    for i in range(6):
        records += saga(f"bad{i}", ("email.send", {"to": "a@b.com"}), fail_last=True)

    model = FailureModel.fit(records, min_support=3)
    assert model.total == 12
    assert model.failures == 6
    assert model.base_rate == pytest.approx(0.5)


# -- 2. lift, not raw rate ---------------------------------------------------------------

def test_lift_is_relative_to_the_base_rate_not_an_absolute_frequency():
    """In a log where most things fail, a 75%-failing feature is unremarkable."""
    records = []
    for i in range(30):
        records += saga(f"bad{i}", ("flaky.tool", {"k": 1}), fail_last=True)
    for i in range(10):
        records += saga(f"ok{i}", ("flaky.tool", {"k": 1}))

    model = FailureModel.fit(records, min_support=5)
    assessment = model.assess("flaky.tool", {"k": 1})

    assert model.base_rate == pytest.approx(0.75)
    tool_factor = next(f for f in assessment.supported if f.feature == "tool=flaky.tool")
    assert tool_factor.rate == pytest.approx(0.75)
    assert tool_factor.lift == pytest.approx(1.0)     # exactly average, not "75% risk"
    assert not assessment.elevated(2.0)


def test_a_genuinely_elevated_feature_shows_lift_above_one():
    records = []
    # baseline: small amounts almost always succeed
    for i in range(40):
        records += saga(f"ok{i}", ("stripe.charge", {"amount": 100 + i}))
    # large amounts almost always fail
    for i in range(10):
        records += saga(f"big{i}", ("stripe.charge", {"amount": 900000 + i}),
                        fail_last=True)

    model = FailureModel.fit(records, min_support=5)
    risky = model.assess("stripe.charge", {"amount": 950000})
    safe = model.assess("stripe.charge", {"amount": 120})

    assert risky.elevated(2.0), risky.format_text()
    assert any("top decile" in f.feature for f in risky.supported)
    assert not safe.elevated(2.0)
    assert risky.max_lift > safe.max_lift


# -- 3. small samples produce silence, not confidence -------------------------------------

def test_below_the_support_threshold_no_number_is_produced():
    """'1 of 1 similar calls failed' is not a 100% risk."""
    records = saga("only", ("rare.tool", {"k": 1}), fail_last=True)
    model = FailureModel.fit(records, min_support=5)
    assessment = model.assess("rare.tool", {"k": 1})

    assert not assessment.sufficient_evidence
    assert assessment.max_lift is None
    assert not assessment.elevated(1.5)
    report = " ".join(assessment.format_text().split())
    assert "INSUFFICIENT EVIDENCE" in report


def test_insufficient_evidence_is_not_presented_as_safety():
    """The distinction this package keeps making: absence of data is not
    absence of risk."""
    records = saga("only", ("rare.tool", {"k": 1}), fail_last=True)
    model = FailureModel.fit(records, min_support=5)
    report = " ".join(model.assess("rare.tool", {"k": 1}).format_text().split())
    assert "not a clean bill of health" in report
    assert "the log cannot say" in report


def test_an_empty_log_predicts_nothing_and_says_so():
    model = FailureModel.fit([])
    assert model.total == 0
    assert model.base_rate == 0.0
    assert "predicts nothing" in model.format_text()

    assessment = model.assess("any.tool", {})
    assert not assessment.sufficient_evidence
    assert "no history" in assessment.format_text()


def test_a_log_with_no_failures_reports_no_elevated_risk():
    records = []
    for i in range(20):
        records += saga(f"ok{i}", ("safe.tool", {"k": 1}))

    model = FailureModel.fit(records, min_support=5)
    assessment = model.assess("safe.tool", {"k": 1})
    assert model.failures == 0
    assert not assessment.elevated(1.5)
    assert assessment.max_lift == 0.0        # base rate is zero; lift cannot inflate


def test_min_support_is_validated():
    with pytest.raises(ValueError, match="min_support must be >= 1"):
        FailureModel(min_support=0)


# -- explainability ------------------------------------------------------------------------

def test_features_read_as_sentences_an_operator_can_act_on():
    records = []
    for i in range(40):
        records += saga(f"ok{i}", ("stripe.charge", {"amount": 10 + i, "currency": "GBP"}))
    for i in range(10):
        records += saga(f"big{i}", ("stripe.charge", {"amount": 900000 + i,
                                                     "currency": "GBP"}),
                        fail_last=True)

    model = FailureModel.fit(records, min_support=5)
    features = [f.feature for f in model.assess(
        "stripe.charge", {"amount": 950000, "currency": "GBP"}).supported]

    assert "tool=stripe.charge" in features
    assert any("args=[amount,currency]" in f for f in features)
    assert any("amount in top decile" in f for f in features)


def test_argument_shape_is_a_feature():
    """A call missing a field the successful ones all had is worth noticing."""
    records = []
    for i in range(20):
        records += saga(f"ok{i}", ("api.call", {"id": i, "token": "t"}))
    for i in range(20):
        records += saga(f"bad{i}", ("api.call", {"id": i}), fail_last=True)

    model = FailureModel.fit(records, min_support=5)
    without_token = model.assess("api.call", {"id": 1})
    with_token = model.assess("api.call", {"id": 1, "token": "t"})

    assert without_token.max_lift > with_token.max_lift


def test_the_report_says_it_is_a_correlation_not_a_verdict():
    records = []
    for i in range(20):
        records += saga(f"ok{i}", ("t", {"k": 1}))
    for i in range(20):
        records += saga(f"bad{i}", ("t", {"k": 2}), fail_last=True)

    model = FailureModel.fit(records, min_support=5)
    report = " ".join(model.assess("t", {"k": 2}).format_text().split())
    assert "correlation" in report
    assert "not a verdict" in report
    assert "do not let it silently refuse real work" in report


def test_the_assessment_is_machine_readable():
    records = []
    for i in range(20):
        records += saga(f"bad{i}", ("t", {"k": 1}), fail_last=True)
    for i in range(20):
        records += saga(f"ok{i}", ("u", {"k": 1}))

    data = FailureModel.fit(records, min_support=5).assess("t", {"k": 1}).describe()
    assert data["tool"] == "t"
    assert data["sufficient_evidence"] is True
    assert data["max_lift"] > 1.0
    assert data["factors"]


# -- 4. no silent blocking -----------------------------------------------------------------

def test_review_routing_is_opt_in_and_explains_itself():
    """Provided as an explicit hook, never auto-wired: a statistical control
    installed silently into a fail-closed boundary would decline real work for
    reasons nobody can audit."""
    from types import SimpleNamespace

    from agent_saga.gate import PreFlightViolation

    records = []
    for i in range(40):
        records += saga(f"ok{i}", ("stripe.charge", {"amount": 10 + i}))
    for i in range(20):
        records += saga(f"big{i}", ("stripe.charge", {"amount": 900000 + i}),
                        fail_last=True)

    model = FailureModel.fit(records, min_support=5)
    rule = require_review_above(model, threshold=2.0)

    rule(SimpleNamespace(tool="stripe.charge", kwargs={"amount": 20}))   # passes

    with pytest.raises(PreFlightViolation) as excinfo:
        rule(SimpleNamespace(tool="stripe.charge", kwargs={"amount": 950000}))
    message = " ".join(str(excinfo.value).split())
    assert "correlation from history" in message
    assert "not a defect found in this call" in message
    assert "a human decides" in message
    # REQUIRE_APPROVAL, not BLOCK: a resemblance warrants a look, not a refusal
    assert excinfo.value.decision.verdict.value == "REQUIRE_APPROVAL"


def test_review_routing_stays_quiet_without_evidence():
    from types import SimpleNamespace

    model = FailureModel.fit(saga("only", ("t", {"k": 1}), fail_last=True),
                             min_support=5)
    rule = require_review_above(model, threshold=1.1)
    rule(SimpleNamespace(tool="t", kwargs={"k": 1}))      # no raise: no evidence
