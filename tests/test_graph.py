"""Graph export: the rollback fork, drawn honestly.

The claims under test:
1. A dirty rollback never draws like a clean one -- compensated, failed, and
   orphaned get distinct styles. This is RollbackReport.clean in pixels.
2. Reconstruction is total: empty, truncated, hostile, and wrong-typed records
   render a valid diagram, never an exception.
3. User data never becomes syntax -- a tool named to break Mermaid or DOT
   produces a funny label, not an injected diagram.
4. Output is deterministic, so a diagram can be committed and diffed.
5. DAG plans render with live status, and a dependency on an unregistered node
   is drawn rather than silently dropped.
6. It works on a real saga's WAL, end to end.
"""

import pytest
from conftest import aio

from agent_saga import ActionSemantics, AsyncWAL, Compensation, SagaAborted, saga_scope
from agent_saga.dag import DAGSaga
from agent_saga.graph import dag_to_dot, dag_to_mermaid, wal_to_dot, wal_to_mermaid


def rec(event, **kw):
    return {"event": event, **kw}


CLEAN_ROLLBACK = [
    rec("SAGA_START", saga_id="s-1", name="checkout"),
    rec("STEP_INTENT", saga_id="s-1", step_id="a", tool="stripe.charge",
        semantics="COMPENSABLE"),
    rec("STEP_COMMITTED", saga_id="s-1", step_id="a", tool="stripe.charge",
        semantics="COMPENSABLE"),
    rec("ROLLBACK_START", saga_id="s-1"),
    rec("COMPENSATED", saga_id="s-1", step_id="a", tool="stripe.charge"),
    rec("SAGA_ABORTED", saga_id="s-1"),
]

DIRTY_ROLLBACK = [
    rec("SAGA_START", saga_id="s-2", name="checkout"),
    rec("STEP_INTENT", saga_id="s-2", step_id="a", tool="stripe.charge",
        semantics="COMPENSABLE"),
    rec("STEP_COMMITTED", saga_id="s-2", step_id="a", tool="stripe.charge",
        semantics="COMPENSABLE"),
    rec("STEP_INTENT", saga_id="s-2", step_id="b", tool="email.send",
        semantics="IRREVERSIBLE"),
    rec("STEP_COMMITTED", saga_id="s-2", step_id="b", tool="email.send",
        semantics="IRREVERSIBLE"),
    rec("ROLLBACK_START", saga_id="s-2"),
    rec("STEP_ORPHANED", saga_id="s-2", step_id="b", tool="email.send",
        semantics="IRREVERSIBLE"),
    rec("COMPENSATION_FAILED", saga_id="s-2", step_id="a", tool="stripe.charge",
        error="ConnectionError('refund endpoint down')", attempts=3),
    rec("SAGA_ABORTED", saga_id="s-2"),
]


# -- 1. a dirty rollback never draws like a clean one ------------------------------

def test_clean_and_dirty_rollbacks_render_differently():
    clean = wal_to_mermaid(CLEAN_ROLLBACK)
    dirty = wal_to_mermaid(DIRTY_ROLLBACK)

    assert "compensated" in clean
    assert "class c0 undo" in clean          # the calm colour on the undo node
    # The saga still ABORTED -- that terminal node is legitimately alarming.
    # What must NOT be alarming is the compensation itself.
    assert not any("c0" in ln and ln.endswith(" bad")
                   for ln in clean.splitlines() if ln.strip().startswith("class "))

    assert "COMPENSATION FAILED" in dirty
    assert "ORPHANED" in dirty
    assert "needs a human" in dirty
    assert "no undo exists" in dirty
    # both failure kinds are styled `bad`, and neither is styled `undo`
    applied = [ln for ln in dirty.splitlines() if ln.strip().startswith("class ")]
    bad_line = next(ln for ln in applied if ln.endswith(" bad"))
    assert "c0" in bad_line and "c1" in bad_line
    assert not any(ln.endswith(" undo") for ln in applied)


def test_the_failure_reason_travels_into_the_diagram():
    dirty = wal_to_mermaid(DIRTY_ROLLBACK)
    assert "refund endpoint down" in dirty


def test_an_unknown_outcome_is_flagged_not_assumed_committed():
    records = [
        rec("SAGA_START", saga_id="s-3"),
        rec("STEP_INTENT", step_id="a", tool="stripe.charge", semantics="COMPENSABLE"),
        rec("STEP_UNKNOWN", step_id="a", tool="stripe.charge",
            semantics="COMPENSABLE", error="TimeoutError()"),
    ]
    out = wal_to_mermaid(records)
    assert "UNKNOWN - effect may have landed" in out
    warn_line = next(ln for ln in out.splitlines()
                     if ln.strip().startswith("class ") and ln.endswith(" warn"))
    assert "n0" in warn_line


def test_a_log_with_no_terminal_record_says_so():
    """A saga whose process was SIGKILLed has no SAGA_COMPLETE/ABORTED. The
    diagram must not imply it finished."""
    out = wal_to_mermaid(CLEAN_ROLLBACK[:3])
    assert "no terminal record" in out
    assert "process died or log truncated" in out


# -- 2. totality -------------------------------------------------------------------

@pytest.mark.parametrize("records", [
    [],
    None,
    [None, 42, "not a record"],
    [{"no_event_key": True}],
    [rec("STEP_COMMITTED")],                            # no tool, no step_id
    [rec("STEP_COMMITTED", step_id={"weird": 1}, tool=["also", "weird"])],
    [rec("SAGA_START", saga_id=None, name=None), rec("SAGA_ABORTED")],
    [rec("COMPENSATED", step_id="ghost", tool="never.committed")],
    123,                                                 # not iterable at all
])
def test_malformed_logs_render_instead_of_raising(records):
    mermaid, dot = wal_to_mermaid(records), wal_to_dot(records)
    assert mermaid.startswith("flowchart TD")
    assert dot.startswith("digraph saga {") and dot.rstrip().endswith("}")


def test_an_empty_log_says_it_is_empty_rather_than_drawing_nothing():
    out = wal_to_mermaid([])
    assert "no step records found" in out


def test_a_wal_without_step_ids_still_correlates_by_tool():
    """Older logs predate step_id. They must degrade, not vanish."""
    records = [
        rec("STEP_COMMITTED", tool="stripe.charge", semantics="COMPENSABLE"),
        rec("COMPENSATED", tool="stripe.charge"),
    ]
    out = wal_to_mermaid(records)
    assert "stripe.charge" in out
    assert "compensated" in out
    assert out.count('n0["') == 1          # one step, not two


# -- 3. user data never becomes syntax ----------------------------------------------

HOSTILE = '"] --> evil["pwned'


def test_a_hostile_tool_name_cannot_author_the_mermaid_diagram():
    out = wal_to_mermaid([rec("STEP_COMMITTED", step_id="a", tool=HOSTILE)])
    assert "--> evil" not in out
    assert "#quot;" in out                              # the quote was escaped
    assert "&#93;" in out                               # and the bracket
    # exactly the edges we authored: start->n0 and n0->done
    assert len([ln for ln in out.splitlines() if "-->" in ln and "classDef" not in ln]) == 2


def test_a_hostile_tool_name_cannot_author_the_dot_diagram():
    """The payload may appear -- inside an escaped label, which is harmless.
    What must not happen is it becoming a second node declaration, so assert
    on the node count rather than on the substring."""
    out = wal_to_dot([rec("STEP_COMMITTED", step_id="a", tool='x" ]; evil [label="p')])
    assert '\\"' in out                                 # the quotes were escaped
    assert out.count("digraph") == 1
    declarations = [ln for ln in out.splitlines() if "[label=" in ln]
    assert len(declarations) == 3                       # start, n0, done -- no evil
    assert all(ln.strip().split(" ")[0] in {"start", "n0", "done"}
               for ln in declarations)


def test_identifiers_are_synthetic_not_user_text():
    out = wal_to_mermaid([rec("STEP_COMMITTED", step_id="a", tool="my.tool")])
    assert 'n0["' in out                                # synthetic id
    assert "my.tool" in out                             # user text only in label


# -- 4. determinism ------------------------------------------------------------------

def test_output_is_byte_identical_across_runs():
    assert wal_to_mermaid(DIRTY_ROLLBACK) == wal_to_mermaid(DIRTY_ROLLBACK)
    assert wal_to_dot(DIRTY_ROLLBACK) == wal_to_dot(DIRTY_ROLLBACK)


# -- 5. DAG plans -------------------------------------------------------------------

def test_dag_plan_renders_nodes_edges_and_status():
    dag = DAGSaga(name="vacation")
    dag.add_node("flight", lambda ctx: None, description="book the flight")
    dag.add_node("hotel", lambda ctx: None, dependencies=["flight"])
    dag.nodes["flight"].status = "COMPLETED"
    dag.nodes["hotel"].status = "FAILED"
    dag.nodes["hotel"].error = "no rooms"

    out = dag_to_mermaid(dag)
    assert "book the flight" in out
    assert "no rooms" in out
    assert "n0 --> n1" in out                           # the dependency edge
    assert "class n0 ok" in out
    assert "class n1 bad" in out

    dot = dag_to_dot(dag)
    assert "n0 -> n1;" in dot
    assert dot.rstrip().endswith("}")


def test_a_dependency_on_an_unregistered_node_is_drawn_not_dropped():
    """The plan would raise at sort time. An invisible broken edge is how a
    bad plan looks fine."""
    dag = DAGSaga(name="broken")
    dag.add_node("hotel", lambda ctx: None, dependencies=["flight"])   # never added

    out = dag_to_mermaid(dag)
    assert "MISSING" in out
    assert "flight" in out
    assert dag_to_dot(dag).count("MISSING") == 1


def test_an_empty_dag_and_a_non_dag_object_both_render():
    assert "no nodes registered" in dag_to_mermaid(DAGSaga(name="empty"))
    assert "no nodes registered" in dag_to_mermaid(object())


# -- the CLI ---------------------------------------------------------------------------

def _write_wal(path, records):
    import json
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def test_cli_graph_renders_and_writes_a_file(tmp_path, capsys):
    from agent_saga.cli import main

    wal = tmp_path / "w.wal"
    _write_wal(wal, DIRTY_ROLLBACK)

    assert main(["graph", "--wal", str(wal)]) == 0
    assert "flowchart TD" in capsys.readouterr().out

    out = tmp_path / "d.dot"
    assert main(["graph", "--wal", str(wal), "--format", "dot", "-o", str(out)]) == 0
    assert out.read_text(encoding="utf-8").startswith("digraph saga {")


def test_cli_graph_selects_the_named_saga_and_refuses_an_unknown_one(tmp_path, capsys):
    from agent_saga.cli import main

    wal = tmp_path / "w.wal"
    _write_wal(wal, CLEAN_ROLLBACK + DIRTY_ROLLBACK)

    assert main(["graph", "--wal", str(wal), "--saga", "s-1"]) == 0
    out = capsys.readouterr().out
    assert "compensated" in out and "COMPENSATION FAILED" not in out

    # naming a saga that isn't there must fail loudly, not draw the wrong one
    assert main(["graph", "--wal", str(wal), "--saga", "nope"]) == 1
    assert "no saga 'nope'" in capsys.readouterr().out


def test_cli_graph_keeps_records_that_carry_no_saga_id(tmp_path, capsys):
    """Absence of a saga id is not evidence of belonging elsewhere. An older or
    truncated log must still draw its steps -- silently filtering them would
    render 'no step records found' for a log plainly full of steps."""
    from agent_saga.cli import main

    wal = tmp_path / "w.wal"
    _write_wal(wal, [
        rec("SAGA_START", saga_id="s-1", name="legacy"),
        rec("STEP_COMMITTED", step_id="a", tool="legacy.tool"),   # no saga_id
        rec("SAGA_COMPLETE", saga_id="s-1"),
    ])

    assert main(["graph", "--wal", str(wal), "--saga", "s-1"]) == 0
    captured = capsys.readouterr()
    assert "legacy.tool" in captured.out
    assert "no step records found" not in captured.out
    assert "carry no saga id" in captured.err          # and it says so


def test_cli_graph_on_a_missing_wal_reports_instead_of_tracebacking(tmp_path, capsys):
    from agent_saga.cli import main

    assert main(["graph", "--wal", str(tmp_path / "absent.wal")]) == 2
    assert "cannot read" in capsys.readouterr().out


# -- 6. end to end on a real saga ------------------------------------------------------

@aio
async def test_a_real_rollback_draws_its_fork(tmp_path):
    undo = []

    def charge(amount):
        return {"id": "ch_1", "amount": amount}

    def boom():
        raise RuntimeError("shipping provider down")

    wal = AsyncWAL(tmp_path / "wal.jsonl")
    await wal.start()
    try:
        with pytest.raises(SagaAborted):
            async with saga_scope(wal=wal, name="checkout") as saga:
                await saga.execute(
                    tool="stripe.charge", semantics=ActionSemantics.COMPENSABLE,
                    forward=charge, forward_kwargs={"amount": 4200},
                    compensate=lambda r: Compensation(
                        fn=lambda charge_id: undo.append(charge_id),
                        kwargs={"charge_id": r["id"]}, description="refund"))
                await saga.execute(
                    tool="ship.order", semantics=ActionSemantics.COMPENSABLE,
                    forward=boom)
        records = await wal.read_all()
    finally:
        await wal.close()

    assert undo == ["ch_1"]                             # the rollback really ran
    out = wal_to_mermaid(records)

    assert "stripe.charge" in out and "ship.order" in out
    assert "compensated" in out                         # the fork off step 1
    assert "SAGA_ABORTED" in out
    assert "shipping provider down" in out              # the abort cause
    assert "-.->" in out                                # a real branch was drawn
