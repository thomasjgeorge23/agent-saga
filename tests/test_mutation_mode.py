"""ChaosRunner mutation mode (#32): corrupt compensation kwargs and verify the
handler refuses bad data rather than silently acting on it."""

from conftest import aio

from agent_saga import saga_scope, ActionSemantics
from agent_saga.context import Compensation
from agent_saga.testing import ChaosRunner, MutationResult, default_mutations
from agent_saga.schemas import SchemaContractError


def _saga_with_compensation(fn):
    async def saga(ctx):
        await ctx.execute(
            tool="stripe.charge", semantics=ActionSemantics.COMPENSABLE,
            forward=lambda: {"id": "ch_1"},
            compensate=lambda r: Compensation(
                fn=fn, handler="stripe.refund", kwargs={"charge_id": "ch_1", "amount": 100}))
    return saga


@aio
async def test_mutation_flags_fragile_compensation():
    # Ignores its kwargs entirely -> silently "succeeds" on corrupted input.
    def fragile(**kw): return "ok"
    result = await ChaosRunner(mutation_mode=True).mutation_run(_saga_with_compensation(fragile))
    assert isinstance(result, MutationResult)
    assert result.tested == 6                 # 2 kwargs x (wrong/null/drop)
    assert not result.all_robust
    assert len(result.fragile) == 6


@aio
async def test_mutation_passes_schema_validated_compensation():
    def robust(charge_id=None, amount=None, **kw):
        if not isinstance(charge_id, str) or not charge_id.startswith("ch_"):
            raise SchemaContractError(f"bad charge_id {charge_id!r}")
        if not isinstance(amount, (int, float)) or amount < 0:
            raise SchemaContractError(f"bad amount {amount!r}")
        return "refunded"
    result = await ChaosRunner(mutation_mode=True).mutation_run(_saga_with_compensation(robust))
    assert result.all_robust
    assert all(o.schema_validated for o in result.outcomes)


@aio
async def test_mutation_treats_any_raise_as_robust():
    # Validates both fields and raises a plain assertion/TypeError on corruption
    # -> refused bad data (robust), though not via SchemaContractError.
    def defensive(charge_id, amount):
        assert isinstance(charge_id, str) and charge_id.startswith("ch_")
        assert isinstance(amount, (int, float)) and amount >= 0
        return "ok"
    result = await ChaosRunner(mutation_mode=True).mutation_run(_saga_with_compensation(defensive))
    assert result.all_robust
    assert not any(o.schema_validated for o in result.outcomes)


@aio
async def test_mutation_run_via_run_dispatch():
    def fragile(**kw): return "ok"
    # mutation_mode routes run() to mutation_run(), returning a MutationResult
    result = await ChaosRunner(mutation_mode=True).run(_saga_with_compensation(fragile))
    assert isinstance(result, MutationResult)


def test_default_mutations_covers_wrong_null_drop():
    muts = list(default_mutations({"charge_id": "ch_1", "amount": 100}))
    descs = {d for d, _ in muts}
    assert "wrong charge_id" in descs and "null amount" in descs and "drop charge_id" in descs
    # a dropped field is actually absent
    dropped = dict(muts)["drop charge_id"]
    assert "charge_id" not in dropped
