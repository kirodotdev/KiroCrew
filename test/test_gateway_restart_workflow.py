"""Process supervision smoke; this is not proof of private namespace execution."""

import os

import pytest
from e2e.test_gateway_boot_matrix import _booted, _Client
from e2e.test_private_workflow_memory import _finished

pytestmark = pytest.mark.skipif(
    not os.environ.get("KIROCREW_E2E"),
    reason="Set KIROCREW_E2E=1 for real gateway process supervision",
)


def test_gateway_restart_retains_home_and_replays_workflow():
    source = 'META = {"name": "restart smoke"}\nasync def workflow(ctx):\n    return "restored"\n'
    with _booted("rich") as (handle, client):
        started = client.post("/api/workflows/run", {"source": source})
        assert _finished(client, started["run_id"])["result"] == "restored"
        old_pid, home = handle.proc.pid, handle.home
        current = handle.restart()
        assert handle.proc.poll() is not None
        assert current.proc.pid != old_pid
        assert current.home == home
        client = _Client(current.port, current.token)
        client.diagnostics = current.diagnostics
        assert client.get(f"/api/workflows/runs/{started['run_id']}")["result"] == "restored"
        rerun = client.post(f"/api/workflows/runs/{started['run_id']}/rerun", {})
        assert rerun["run_id"] != started["run_id"]
        assert _finished(client, rerun["run_id"])["result"] == "restored"
        previous = current
        current = current.restart()
        assert previous.proc.poll() is not None
        assert current.proc.pid not in {old_pid, previous.proc.pid}
        assert current.home == home
        client = _Client(current.port, current.token)
        assert client.get(f"/api/workflows/runs/{rerun['run_id']}")["result"] == "restored"
    assert current.proc.poll() is not None
    assert not home.exists()
    with pytest.raises(Exception, match="no longer active"):
        current.restart()
