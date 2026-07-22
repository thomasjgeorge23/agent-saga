"""Connector reconcilers: does the external system agree with the log?

Each of these is written around a way the compensation can report success while
the world disagrees -- a partial refund, a trigger that rewrote the field back,
a third party who edited the row in between. If the API response were
trustworthy, none of this would need to exist.
"""

import sys
import types

import pytest

from conftest import aio

from agent_saga.reconcile import Observation


# ---------------------------------------------------------------------------
# Stripe
# ---------------------------------------------------------------------------

class FakeCharge(dict):
    pass


def install_fake_stripe(monkeypatch, charge=None, error=None):
    module = types.ModuleType("stripe")
    module.api_key = None

    class Charge:
        @staticmethod
        def retrieve(charge_id):
            if error:
                raise error
            return charge

    module.Charge = Charge
    module.Refund = types.SimpleNamespace(create=lambda **kw: {})
    monkeypatch.setitem(sys.modules, "stripe", module)
    return module


@pytest.fixture
def stripe_creds(monkeypatch):
    from agent_saga.connectors import _secrets

    monkeypatch.setattr(_secrets, "_RESOLVER", lambda ref: "sk_test", raising=False)
    from agent_saga.connectors import set_credential_resolver

    set_credential_resolver(lambda ref: "sk_test")
    yield
    set_credential_resolver(None)


def test_a_refunded_charge_reads_as_reversed(monkeypatch, stripe_creds):
    from agent_saga.connectors.stripe import observe_refund

    install_fake_stripe(monkeypatch, FakeCharge(
        refunded=True, amount=4200, amount_refunded=4200, status="succeeded"))
    obs = observe_refund(charge_id="ch_1", credential_ref="stripe_prod")
    assert obs.reversed_ is True and obs.exists is True and obs.amount == 4200


def test_a_charge_still_standing_reads_as_not_reversed(monkeypatch, stripe_creds):
    """The compensation returned success and the money did not go back."""
    from agent_saga.connectors.stripe import observe_refund

    install_fake_stripe(monkeypatch, FakeCharge(
        refunded=False, amount=4200, amount_refunded=0, status="succeeded"))
    obs = observe_refund(charge_id="ch_1", credential_ref="stripe_prod")
    assert obs.reversed_ is False and obs.exists is True


def test_a_partial_refund_is_not_a_reversal(monkeypatch, stripe_creds):
    """Calling a half-returned payment 'confirmed' is exactly the report an
    auditor would catch."""
    from agent_saga.connectors.stripe import observe_refund

    install_fake_stripe(monkeypatch, FakeCharge(
        refunded=False, amount=4200, amount_refunded=1000, status="succeeded"))
    obs = observe_refund(charge_id="ch_1", credential_ref="stripe_prod")
    assert obs.reversed_ is False
    assert "PARTIALLY" in obs.detail and "1000 of 4200" in obs.detail


def test_a_charge_stripe_has_never_heard_of(monkeypatch, stripe_creds):
    from agent_saga.connectors.stripe import observe_refund

    install_fake_stripe(monkeypatch, error=Exception("No such charge: ch_1"))
    obs = observe_refund(charge_id="ch_1", credential_ref="stripe_prod")
    assert obs.exists is False and obs.reversed_ is False


def test_a_transport_error_propagates_and_becomes_unverifiable(monkeypatch, stripe_creds):
    """Reporting an unreachable Stripe as 'not refunded' would manufacture
    drift out of a network blip."""
    from agent_saga.connectors.stripe import observe_refund

    install_fake_stripe(monkeypatch, error=ConnectionError("stripe unreachable"))
    with pytest.raises(ConnectionError):
        observe_refund(charge_id="ch_1", credential_ref="stripe_prod")


def test_the_reconciler_tolerates_the_full_compensation_kwargs(monkeypatch, stripe_creds):
    """The registry passes a compensation's kwargs through unchanged, so a
    reconciler kept in signature-lockstep would break the first time either
    side gained an argument."""
    from agent_saga.connectors.stripe import observe_refund

    install_fake_stripe(monkeypatch, FakeCharge(
        refunded=True, amount=100, amount_refunded=100, status="succeeded"))
    obs = observe_refund(charge_id="ch_1", amount=100, idempotency_key="k",
                         credential_ref="stripe_prod", something_new="later")
    assert obs.reversed_ is True


# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_pg(monkeypatch):
    """Intercept at _run, so the SQL builders and identifier allowlist still
    execute -- a fake that replaced the whole module would test nothing."""
    from agent_saga.connectors import postgres

    state = {"row": None, "queries": []}

    async def fake_run(credential_ref, plan):
        async def execute(sql, params):
            state["queries"].append(sql)
            return 1

        async def fetchone(sql, params):
            state["queries"].append(sql)
            return state["row"]

        return await plan("numeric", execute, fetchone)

    monkeypatch.setattr(postgres, "_run", fake_run)
    return state


@aio
async def test_a_row_back_at_its_previous_values_is_reversed(fake_pg):
    from agent_saga.connectors.postgres import observe_restored_row

    fake_pg["row"] = {"status": "lead", "owner": "alice"}
    obs = await observe_restored_row(
        table="accounts", pk_column="id", pk_value=7,
        previous_state={"status": "lead", "owner": "alice"},
        expected_current={"status": "customer", "owner": "bob"},
        credential_ref="pg")
    assert obs.reversed_ is True


@aio
async def test_a_row_still_holding_the_agents_write_is_not_reversed(fake_pg):
    from agent_saga.connectors.postgres import observe_restored_row

    fake_pg["row"] = {"status": "customer", "owner": "bob"}
    obs = await observe_restored_row(
        table="accounts", pk_column="id", pk_value=7,
        previous_state={"status": "lead", "owner": "alice"},
        expected_current={"status": "customer", "owner": "bob"},
        credential_ref="pg")
    assert obs.reversed_ is False
    assert "still holds the values the agent wrote" in obs.detail


@aio
async def test_a_row_a_third_party_rewrote_is_indeterminate(fake_pg):
    """Neither state. A boolean would have to guess, and either guess is wrong
    in a real case."""
    from agent_saga.connectors.postgres import observe_restored_row

    fake_pg["row"] = {"status": "churned", "owner": "carol"}
    obs = await observe_restored_row(
        table="accounts", pk_column="id", pk_value=7,
        previous_state={"status": "lead", "owner": "alice"},
        expected_current={"status": "customer", "owner": "bob"},
        credential_ref="pg")
    assert obs.reversed_ is None
    assert "third party" in obs.detail


@aio
async def test_a_vanished_row_is_reported(fake_pg):
    from agent_saga.connectors.postgres import observe_restored_row

    fake_pg["row"] = None
    obs = await observe_restored_row(
        table="accounts", pk_column="id", pk_value=7,
        previous_state={"status": "lead"}, expected_current={"status": "customer"},
        credential_ref="pg")
    assert obs.exists is False and obs.reversed_ is False


@aio
async def test_an_inserted_row_that_was_deleted_is_reversed(fake_pg):
    from agent_saga.connectors.postgres import observe_deleted_insert

    fake_pg["row"] = None
    obs = await observe_deleted_insert(table="accounts", pk={"id": 7},
                                       credential_ref="pg")
    assert obs.reversed_ is True and obs.exists is False


@aio
async def test_an_inserted_row_still_present_is_not_reversed(fake_pg):
    from agent_saga.connectors.postgres import observe_deleted_insert

    fake_pg["row"] = {"id": 7}
    obs = await observe_deleted_insert(table="accounts", pk={"id": 7},
                                       credential_ref="pg")
    assert obs.reversed_ is False


@aio
async def test_a_reinserted_row_must_carry_its_original_values(fake_pg):
    """A row with the right key but different values is not a restoration."""
    from agent_saga.connectors.postgres import observe_reinserted_row

    original = {"id": 7, "status": "lead", "owner": "alice"}

    fake_pg["row"] = dict(original)
    assert (await observe_reinserted_row(
        table="accounts", row=original, pk_columns=["id"],
        credential_ref="pg")).reversed_ is True

    fake_pg["row"] = {"id": 7, "status": "customer", "owner": "alice"}
    obs = await observe_reinserted_row(table="accounts", row=original,
                                       pk_columns=["id"], credential_ref="pg")
    assert obs.reversed_ is None and "values differ" in obs.detail

    fake_pg["row"] = None
    assert (await observe_reinserted_row(
        table="accounts", row=original, pk_columns=["id"],
        credential_ref="pg")).reversed_ is False


@aio
async def test_the_identifier_allowlist_still_applies_to_reads(fake_pg):
    """The reconciler builds SQL from LLM-influenced table names too, so it
    must not be the hole in the boundary the connector maintains."""
    from agent_saga.connectors.postgres import observe_deleted_insert

    with pytest.raises(Exception):
        await observe_deleted_insert(table='accounts"; DROP TABLE users; --',
                                     pk={"id": 1}, credential_ref="pg")


# ---------------------------------------------------------------------------
# Salesforce
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.params = None

    async def get(self, url, headers=None, params=None):
        self.params = params
        return self.response

    async def aclose(self):
        pass


@pytest.fixture
def sf_creds():
    from agent_saga.connectors import set_credential_resolver

    set_credential_resolver(lambda ref: "token")
    yield
    set_credential_resolver(None)


@aio
async def test_reverted_fields_holding_their_old_values(sf_creds):
    from agent_saga.connectors.salesforce import observe_reverted_object

    client = FakeClient(FakeResponse({"Status": "Lead", "Owner": "alice"}))
    obs = await observe_reverted_object(
        instance_url="https://x.my.salesforce.com", object_type="Account",
        object_id="001x", previous_values={"Status": "Lead", "Owner": "alice"},
        credential_ref="sf", client=client)
    assert obs.reversed_ is True


@aio
async def test_a_trigger_that_rewrote_the_field_is_caught(sf_creds):
    """The PATCH returned 204 and a workflow rule immediately put it back.
    Nothing except reading the record would ever notice."""
    from agent_saga.connectors.salesforce import observe_reverted_object

    client = FakeClient(FakeResponse({"Status": "Customer", "Owner": "alice"}))
    obs = await observe_reverted_object(
        instance_url="https://x.my.salesforce.com", object_type="Account",
        object_id="001x", previous_values={"Status": "Lead", "Owner": "alice"},
        credential_ref="sf", client=client)
    assert obs.reversed_ is False
    assert "1 of 2 field(s)" in obs.detail and "Status" in obs.detail


@aio
async def test_only_the_fields_the_saga_touched_are_compared(sf_creds):
    """Reverting a record is not a claim about the rest of it."""
    from agent_saga.connectors.salesforce import observe_reverted_object

    client = FakeClient(FakeResponse({"Status": "Lead", "Unrelated": "changed"}))
    obs = await observe_reverted_object(
        instance_url="https://x.my.salesforce.com", object_type="Account",
        object_id="001x", previous_values={"Status": "Lead"},
        credential_ref="sf", client=client)
    assert obs.reversed_ is True
    assert set(client.params["fields"].split(",")) == {"Status"}


@aio
async def test_a_deleted_record_is_reported(sf_creds):
    from agent_saga.connectors.salesforce import observe_reverted_object

    client = FakeClient(FakeResponse({}, status_code=404))
    obs = await observe_reverted_object(
        instance_url="https://x.my.salesforce.com", object_type="Account",
        object_id="001x", previous_values={"Status": "Lead"},
        credential_ref="sf", client=client)
    assert obs.exists is False and obs.reversed_ is False


@aio
async def test_read_only_fields_are_not_asserted_on(sf_creds):
    """`writable` filters them out of the revert, so asserting on them would
    report drift for a field the compensation never tried to set."""
    from agent_saga.connectors.salesforce import observe_reverted_object

    client = FakeClient(FakeResponse({}))
    obs = await observe_reverted_object(
        instance_url="https://x.my.salesforce.com", object_type="Account",
        object_id="001x", previous_values={"Id": "001x"},
        credential_ref="sf", client=client)
    assert obs.reversed_ is None
    assert "no writable fields" in obs.detail


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_every_shipped_compensator_has_a_reconciler():
    """The registry shipping with no engines in it was the gap this closes."""
    import agent_saga.connectors.postgres  # noqa: F401
    import agent_saga.connectors.salesforce  # noqa: F401
    import agent_saga.connectors.stripe  # noqa: F401
    from agent_saga.reconcile import registered_reconcilers

    registered = registered_reconcilers()
    for handler in ("stripe.refund", "postgres.restore_row",
                    "postgres.delete_inserted_row", "postgres.reinsert_row",
                    "salesforce.revert_object"):
        assert handler in registered, f"{handler} has no reconciler"
