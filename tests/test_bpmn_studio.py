"""BPMN Studio visual designer (#38): generate saga_scope Python from a designed
step list, round-trip through BPMN, and serve it over the dashboard."""

import ast
import json
import threading
import urllib.request
from pathlib import Path

from conftest import aio
from agent_saga.bpmn import (
    generate_saga_code, design_to_bpmn_xml, saga_code_from_bpmn, steps_to_records)
from agent_saga.ui.server import make_server


STEPS = [
    {"tool": "stripe.charge", "semantics": "COMPENSABLE",
     "compensate": {"handler": "stripe.refund", "kwargs": {"charge_id": "ch", "amount": 100}}},
    {"tool": "db.read_user", "semantics": "REVERSIBLE", "compensate": None},
    {"tool": "email.send", "semantics": "IRREVERSIBLE", "compensate": None},
]


def test_generate_saga_code_is_valid_python():
    code = generate_saga_code(STEPS, name="checkout")
    ast.parse(code)                                   # compiles
    assert "async def run_checkout" in code
    # tool is emitted as a repr'd literal (single-quoted) for injection safety
    assert "tool='stripe.charge'" in code


def test_generate_saga_code_is_injection_safe():
    # A tool name crafted to break out of the string must stay inert.
    evil = [{"tool": 'x"; import os; os.system("evil"); "', "semantics": "REVERSIBLE"}]
    code = generate_saga_code(evil, name='n"; BADNAME=1; "')
    tree = ast.parse(code)                            # must still compile
    imports = [a.name for n in ast.walk(tree)
               if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names]
    assert "os" not in imports                        # no injected import
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Assign)]  # no BADNAME=1


def test_only_compensable_steps_get_compensate():
    code = generate_saga_code(STEPS)
    assert code.count("compensate=lambda result: Compensation") == 1
    assert "ActionSemantics.REVERSIBLE" in code
    assert "ActionSemantics.IRREVERSIBLE" in code


def test_empty_steps_is_valid():
    ast.parse(generate_saga_code([], name="empty"))


def test_design_to_bpmn_and_back():
    xml = design_to_bpmn_xml(STEPS)
    assert "bpmn:serviceTask" in xml and "isForCompensation" in xml
    code = saga_code_from_bpmn(xml, name="reimported")
    ast.parse(code)
    assert "stripe.charge" in code
    # stripe.charge had a compensation -> reconstructed as COMPENSABLE
    assert "COMPENSABLE" in code


def test_steps_to_records():
    recs = steps_to_records(STEPS)
    assert recs[0]["event"] == "STEP_COMMITTED" and recs[0]["tool"] == "stripe.charge"
    assert "compensation" in recs[0] and "compensation" not in recs[1]


@aio
async def test_design_endpoints_over_http():
    with __import__("tempfile").TemporaryDirectory() as d:
        p = Path(d) / "w.wal"
        p.write_text("", encoding="utf-8")
        httpd = make_server(str(p), host="127.0.0.1", port=0)
        httpd.daemon_threads = True
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            def post(path, payload):
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}{path}",
                    data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req) as r:
                    return r.read().decode()

            code = post("/api/design/code", {"steps": STEPS, "name": "checkout"})
            assert "async def run_checkout" in code
            xml = post("/api/design/bpmn", {"steps": STEPS})
            assert "serviceTask" in xml
            back = post("/api/design/code-from-bpmn", {"xml": xml})
            assert "stripe.charge" in back
        finally:
            httpd.shutdown()
            httpd.server_close()
