"""Connector tests. No network, no real credentials, no live database."""

import contextlib
import json
import os
import sys
import tempfile
import types
from pathlib import Path

import pytest

from agent_saga import ActionSemantics, AsyncWAL, SagaContext
from agent_saga.connectors import (
    SecretLeak,
    assert_no_secrets,
    resolve_credential,
    set_credential_resolver,
)
from conftest import aio


@contextlib.contextmanager
def fake_module(name: str, module):
    """Connectors import their SDKs lazily, so a fake in sys.modules is enough."""
    old = sys.modules.get(name)
    sys.modules[name] = module
    try:
        yield module
    finally:
        if old is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = old


@contextlib.contextmanager
def credential(ref: str, value: str):
    env = f"AGENT_SAGA_CRED_{ref.upper()}"
    old = os.environ.get(env)
    os.environ[env] = value
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(env, None)
        else:
            os.environ[env] = old


async def _ctx(tmp: Path):
    wal = AsyncWAL(tmp / "wal.jsonl")
    await wal.start()
    return SagaContext(wal=wal), wal


# ==========================================================================
# Credentials -- the finding that would fail a security review
# ==========================================================================

def test_database_uri_with_password_is_rejected_before_it_reaches_the_wal():
    with pytest.raises(SecretLeak, match="database URI"):
        assert_no_secrets({"dsn_value": "postgresql://user:hunter2@db.internal/prod"},
                          where="test")


@pytest.mark.parametrize("value,label", [
    ("sk_live_abcdefghij1234567890", "Stripe"),
    ("ghp_abcdefghijklmnopqrstuvwxyz", "GitHub"),
    ("xoxb-1234567890-abcdefghij", "Slack"),
    ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig", "JWT"),
])
def test_known_credential_shapes_are_rejected(value, label):
    with pytest.raises(SecretLeak):
        assert_no_secrets({"blob": value}, where="test")


def test_credential_shaped_key_names_are_rejected_even_with_innocent_values():
    """A kwarg named `auth_token` is a credential regardless of what today's
    test value happens to look like."""
    with pytest.raises(SecretLeak, match="named like a credential"):
        assert_no_secrets({"auth_token": "placeholder"}, where="test")


def test_reference_suffixed_names_are_allowed():
    assert_no_secrets({"credential_ref": "stripe_prod",
                       "auth_token_ref": "sf_sandbox"}, where="test")


def test_credential_resolves_from_env_and_from_a_custom_store():
    with credential("stripe_prod", "sk_live_xyz"):
        assert resolve_credential("stripe_prod") == "sk_live_xyz"

    set_credential_resolver(lambda ref: f"vault:{ref}")
    try:
        assert resolve_credential("anything") == "vault:anything"
    finally:
        set_credential_resolver(None)


def test_missing_credential_names_the_env_var_and_the_daemon_requirement():
    import agent_saga.connectors._secrets as s
    s._RESOLVER = None
    with pytest.raises(Exception) as exc:
        resolve_credential("nope_missing")
    assert "AGENT_SAGA_CRED_NOPE_MISSING" in str(exc.value)
    assert "daemon" in str(exc.value)


# ==========================================================================
# Stripe
# ==========================================================================

class FakeStripeError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def fake_stripe(*, charge_result=None, refund_error=None, calls=None):
    calls = calls if calls is not None else []
    mod = types.ModuleType("stripe")
    mod.api_key = None

    class Charge:
        @staticmethod
        def create(**kw):
            calls.append(("charge", kw))
            return charge_result or {"id": "ch_test_1", "amount": kw["amount"]}

    class Refund:
        @staticmethod
        def create(**kw):
            calls.append(("refund", kw))
            if refund_error:
                raise refund_error
            return {"id": "re_test_1"}

    mod.Charge, mod.Refund, mod.calls = Charge, Refund, calls
    return mod


@aio
async def test_stripe_charge_rolls_back_with_a_deterministic_refund_key():
    from agent_saga.connectors import stripe as sc

    with tempfile.TemporaryDirectory() as d, credential("stripe_t", "sk_test_x"):
        mod = fake_stripe()
        with fake_module("stripe", mod):
            ctx, wal = await _ctx(Path(d))
            await sc.charge(ctx, customer_id="cus_1", amount=4200,
                            credential_ref="stripe_t")
            report = await ctx.rollback()
            await wal.close()

    assert report.clean
    kinds = [c[0] for c in mod.calls]
    assert kinds == ["charge", "refund"]
    refund_kw = mod.calls[1][1]
    assert refund_kw["charge"] == "ch_test_1"
    assert refund_kw["amount"] == 4200
    # Derived from the charge id alone, so a daemon reproduces it exactly.
    assert refund_kw["idempotency_key"] == sc.refund_key_for("ch_test_1")


@aio
async def test_stripe_already_refunded_is_success_not_failure():
    """Stripe drops idempotency keys after 24h. A daemon returning from a
    two-day outage gets a fresh key and must not loop forever on an error that
    means 'already done'."""
    from agent_saga.connectors import stripe as sc

    with tempfile.TemporaryDirectory() as d, credential("stripe_t", "sk_test_x"):
        mod = fake_stripe(refund_error=FakeStripeError("charge_already_refunded"))
        with fake_module("stripe", mod):
            ctx, wal = await _ctx(Path(d))
            await sc.charge(ctx, customer_id="cus_1", amount=100,
                            credential_ref="stripe_t")
            report = await ctx.rollback()
            await wal.close()
    assert report.clean


@aio
async def test_stripe_secret_never_reaches_the_wal():
    """The compensation descriptor is fsynced in plaintext. It must contain the
    credential's NAME and never its value."""
    from agent_saga.connectors import stripe as sc

    secret = "sk_live_supersecretvalue123456"
    with tempfile.TemporaryDirectory() as d, credential("stripe_t", secret):
        with fake_module("stripe", fake_stripe()):
            ctx, wal = await _ctx(Path(d))
            await sc.charge(ctx, customer_id="cus_1", amount=100,
                            credential_ref="stripe_t")
            await wal.close()
            raw = (Path(d) / "wal.jsonl").read_text(encoding="utf-8")

    assert secret not in raw
    assert "stripe_t" in raw
    descriptor = [json.loads(l) for l in raw.splitlines()
                  if json.loads(l)["event"] == "STEP_COMMITTED"][0]["compensation"]
    assert descriptor["recoverable"] is True
    assert descriptor["kwargs"]["credential_ref"] == "stripe_t"


@aio
async def test_stripe_unknown_outcome_yields_no_compensation_and_is_reported():
    """A charge that timed out has no id. There is nothing to refund by id, so
    the saga must surface it rather than pretend it rolled back."""
    from agent_saga.connectors import stripe as sc

    mod = fake_stripe()

    def boom(**kw):
        raise TimeoutError("stripe timeout")

    mod.Charge.create = staticmethod(boom)

    with tempfile.TemporaryDirectory() as d, credential("stripe_t", "sk_test_x"):
        with fake_module("stripe", mod):
            ctx, wal = await _ctx(Path(d))
            with pytest.raises(TimeoutError):
                await sc.charge(ctx, customer_id="cus_1", amount=100,
                                credential_ref="stripe_t")
            report = await ctx.rollback()
            await wal.close()

    assert not report.clean
    assert [s.tool for s in report.orphaned] == ["stripe.charge"]


# ==========================================================================
# Postgres
# ==========================================================================

class FakePool:
    def __init__(self, row):
        self.row, self.queries = row, []

    @contextlib.asynccontextmanager
    async def acquire(self):
        yield self

    async def fetchrow(self, sql, *args):
        self.queries.append((sql, args))
        return dict(self.row) if self.row else None

    async def execute(self, sql, *args):
        self.queries.append((sql, args))


def fake_psycopg(rowcount=1, executed=None):
    executed = executed if executed is not None else []
    mod = types.ModuleType("psycopg")

    class Cur:
        def __init__(self): self.rowcount = rowcount
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params): executed.append((sql, params))

    class Conn:
        def cursor(self): return Cur()
        def commit(self): pass
        def close(self): pass

    mod.connect = lambda dsn: Conn()
    mod.executed = executed
    return mod


@aio
async def test_postgres_snapshots_only_the_columns_it_changes():
    """SELECT * would drag in generated and identity columns, which are not
    writable on restore."""
    from agent_saga.connectors import postgres as pg

    pool = FakePool({"status": "open", "owner": "ana"})
    with tempfile.TemporaryDirectory() as d, credential("pg_t", "postgresql://h/db"):
        ctx, wal = await _ctx(Path(d))
        await pg.update_row(ctx, pool=pool, table="leads", pk_column="id",
                            pk_value=7, updates={"status": "won", "owner": "bo"},
                            credential_ref="pg_t")
        await wal.close()

    select_sql = pool.queries[0][0]
    assert "SELECT *" not in select_sql
    assert '"status"' in select_sql and '"owner"' in select_sql


@aio
async def test_postgres_restore_refuses_to_clobber_a_concurrent_writer():
    """The guard is the point: a blind UPDATE would silently discard whoever
    wrote after us, turning a rollback into data loss."""
    from agent_saga.connectors import postgres as pg

    with credential("pg_t", "postgresql://h/db"):
        with fake_module("psycopg", fake_psycopg(rowcount=0)):
            with pytest.raises(pg.ConcurrentModification, match="another writer"):
                pg.restore_row(table="leads", pk_column="id", pk_value=7,
                               previous_state={"status": "open"},
                               expected_current={"status": "won"},
                               credential_ref="pg_t")


@aio
async def test_postgres_restore_guards_on_the_values_we_wrote():
    from agent_saga.connectors import postgres as pg

    mod = fake_psycopg(rowcount=1)
    with credential("pg_t", "postgresql://h/db"):
        with fake_module("psycopg", mod):
            pg.restore_row(table="leads", pk_column="id", pk_value=7,
                           previous_state={"status": "open"},
                           expected_current={"status": "won"},
                           credential_ref="pg_t")

    sql, params = mod.executed[0]
    assert "IS NOT DISTINCT FROM" in sql
    assert params == ["open", 7, "won"]


@pytest.mark.parametrize("bad", [
    "leads; DROP TABLE users --",
    'leads" ; DELETE FROM accounts; --',
    "a.b.c",
    "",
])
def test_postgres_rejects_agent_supplied_identifiers_that_are_not_identifiers(bad):
    """An LLM picks the table name at runtime. Identifiers cannot be bound as
    parameters, so this is a live injection surface."""
    from agent_saga.connectors import postgres as pg

    with pytest.raises(ValueError, match="unsafe"):
        pg._qualified(bad)


def test_postgres_accepts_schema_qualified_names():
    from agent_saga.connectors import postgres as pg

    assert pg._qualified("crm.leads") == '"crm"."leads"'


@aio
async def test_postgres_is_compensable_not_reversible():
    """REVERSIBLE steps skip the fsync barrier. Classifying a shared-database
    write as REVERSIBLE would make it unrecoverable after a crash."""
    from agent_saga.connectors import postgres as pg

    pool = FakePool({"status": "open"})
    with tempfile.TemporaryDirectory() as d, credential("pg_t", "postgresql://h/db"):
        ctx, wal = await _ctx(Path(d))
        await pg.update_row(ctx, pool=pool, table="leads", pk_column="id",
                            pk_value=1, updates={"status": "won"},
                            credential_ref="pg_t")
        await wal.close()
        events = [json.loads(l) for l in (Path(d) / "wal.jsonl").read_text(encoding="utf-8").splitlines()]

    assert ctx.stack[0].semantics is ActionSemantics.COMPENSABLE
    assert events[0]["semantics"] == "COMPENSABLE"


# ==========================================================================
# Salesforce
# ==========================================================================

class FakeResponse:
    def __init__(self, payload): self._p = payload
    def json(self): return self._p
    def raise_for_status(self): return None


class FakeAsyncClient:
    def __init__(self, get_payloads): self.get_payloads, self.calls = list(get_payloads), []
    async def get(self, url, headers=None, params=None):
        self.calls.append(("get", params))
        return FakeResponse(self.get_payloads.pop(0))
    async def patch(self, url, json=None, headers=None):
        self.calls.append(("patch", json))
        return FakeResponse({})
    async def aclose(self): pass


def fake_httpx(sync_get=None, sync_calls=None):
    sync_calls = sync_calls if sync_calls is not None else []
    mod = types.ModuleType("httpx")

    class Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, headers=None, params=None):
            sync_calls.append(("get", params))
            return FakeResponse(sync_get or {})
        def patch(self, url, json=None, headers=None):
            sync_calls.append(("patch", json))
            return FakeResponse({})

    mod.Client, mod.AsyncClient, mod.calls = Client, FakeAsyncClient, sync_calls
    return mod


def test_salesforce_strips_fields_that_cannot_be_written():
    """PATCHing a raw GET payload back returns 400
    INVALID_FIELD_FOR_INSERT_UPDATE."""
    from agent_saga.connectors import salesforce as sf

    payload = {"attributes": {"type": "Lead"}, "Id": "00Q1", "Status": "Open",
               "CreatedDate": "2026-01-01", "SystemModstamp": "2026-01-02"}
    assert sf.writable(payload) == {"Status": "Open"}


@aio
async def test_salesforce_reverts_only_the_fields_it_patched():
    """A full-object restore would silently discard a human's concurrent edit
    to a field this saga never touched."""
    from agent_saga.connectors import salesforce as sf

    client = FakeAsyncClient([
        {"Status": "Open", "Rating": "Warm", "LastModifiedDate": "T1"},
        {"LastModifiedDate": "T2"},
    ])
    sync_calls = []
    with tempfile.TemporaryDirectory() as d, credential("sf_t", "tok"):
        with fake_module("httpx", fake_httpx(sync_get={"LastModifiedDate": "T2"},
                                             sync_calls=sync_calls)):
            ctx, wal = await _ctx(Path(d))
            await sf.patch_object(ctx, instance_url="https://acme.my.salesforce.com",
                                  object_type="Lead", object_id="00Q1",
                                  patch={"Status": "Working"},
                                  credential_ref="sf_t", client=client)
            report = await ctx.rollback()
            await wal.close()

    assert report.clean
    reverted = [c for c in sync_calls if c[0] == "patch"][0][1]
    assert reverted == {"Status": "Open"}
    assert "Rating" not in reverted


@aio
async def test_salesforce_refuses_to_revert_a_record_edited_after_us():
    from agent_saga.connectors import salesforce as sf

    with credential("sf_t", "tok"):
        with fake_module("httpx", fake_httpx(sync_get={"LastModifiedDate": "T9"})):
            with pytest.raises(sf.StaleObject, match="modified after this saga"):
                sf.revert_object(instance_url="https://acme.my.salesforce.com",
                                 object_type="Lead", object_id="00Q1",
                                 previous_values={"Status": "Open"},
                                 expected_modstamp="T2", credential_ref="sf_t")


@aio
async def test_salesforce_guard_uses_the_stamp_from_after_our_own_write():
    """Comparing against the pre-write stamp would flag our own change as a
    concurrent edit and block every rollback."""
    from agent_saga.connectors import salesforce as sf

    client = FakeAsyncClient([
        {"Status": "Open", "LastModifiedDate": "T1"},
        {"LastModifiedDate": "T2"},
    ])
    with tempfile.TemporaryDirectory() as d, credential("sf_t", "tok"):
        with fake_module("httpx", fake_httpx()):
            ctx, wal = await _ctx(Path(d))
            await sf.patch_object(ctx, instance_url="https://acme.my.salesforce.com",
                                  object_type="Lead", object_id="00Q1",
                                  patch={"Status": "Working"},
                                  credential_ref="sf_t", client=client)
            await wal.close()

    assert ctx.stack[0].compensation.kwargs["expected_modstamp"] == "T2"


@aio
async def test_salesforce_token_never_reaches_the_wal():
    from agent_saga.connectors import salesforce as sf

    token = "00D5f000000abcDE!AQoAQJ_secret_session_value_here"
    client = FakeAsyncClient([{"Status": "Open", "LastModifiedDate": "T1"},
                              {"LastModifiedDate": "T2"}])
    with tempfile.TemporaryDirectory() as d, credential("sf_t", token):
        with fake_module("httpx", fake_httpx()):
            ctx, wal = await _ctx(Path(d))
            await sf.patch_object(ctx, instance_url="https://acme.my.salesforce.com",
                                  object_type="Lead", object_id="00Q1",
                                  patch={"Status": "Working"},
                                  credential_ref="sf_t", client=client)
            await wal.close()
            raw = (Path(d) / "wal.jsonl").read_text(encoding="utf-8")

    assert token not in raw
    assert "sf_t" in raw
