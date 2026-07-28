import urllib.request
import pytest
from agent_saga.dashboard import DashboardServer
from conftest import aio


def test_dashboard_server_lifecycle():
    server = DashboardServer(host="127.0.0.1", port=8091)
    server.start()

    try:
        # Check GET /
        req = urllib.request.urlopen("http://127.0.0.1:8091/")
        assert req.status == 200
        html = req.read().decode("utf-8")
        assert "agent-saga · Real-Time Telemetry Dashboard" in html

        # Check GET /api/sagas
        req_sagas = urllib.request.urlopen("http://127.0.0.1:8091/api/sagas")
        assert req_sagas.status == 200
    finally:
        server.stop()
