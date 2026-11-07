"""DSH-inspired host work lineage, without duplicating Crew's task scheduler."""
from __future__ import annotations

import json
import subprocess
import sys
import os
import textwrap
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_byo.acp_server import AcpServer
from kiro_byo.config import KiroByoConfig
from kiro_byo.durable_runs import DurableRunStore
from kiro_byo.host_work_link import projected_work_link, validate_work_link, work_link_from_params
from kiro_byo.session_events import bind_work_link, derive_messages, derive_work_link
from kiro_byo.turn_runner import TurnRunnerMixin


VALID = {"taskId": "crew-task.007", "assignmentId": "assign:worker-2", "role": "worker", "parentSessionId": "kiro-abc"}


def _request(server, output, msg_id, method, params):
    server.handle({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
    return next(json.loads(row) for row in reversed(output.getvalue().splitlines()) if json.loads(row).get("id") == msg_id)


def test_host_link_is_opt_in_and_strictly_allowlisted():
    assert work_link_from_params({"_meta": {"kiro": {"customAgents": []}}}) == (False, {})
    assert work_link_from_params({"_meta": {"kiro": {"byoWorkLink": VALID}}}) == (True, VALID)
    assert work_link_from_params({"_meta": {"kiro": {"byoWorkLink": {}}}}) == (True, {})
    for invalid in (
        {"taskId": "../../secret"},
        {"taskId": "a" * 129},
        {"taskId": "some token with spaces"},
        {"role": "system"},
        {"taskId": "task", "grant": "all"},
        {"taskId": 42},
        {"assignmentId": "abc"},
        {"role": "worker"},
        {"parentSessionId": "old-session"},
        {"taskId": "t", "role": "worker", "parentSessionId": "x\nAuthorization: leaked"},
    ):
        with pytest.raises(ValueError):
            validate_work_link(invalid)
        assert projected_work_link(invalid) == {}
    assert projected_work_link({"taskId": "good", "grant": "secret"}) == {}


def test_work_link_is_an_event_projection_not_an_agent_prompt_or_grant():
    session = SimpleNamespace(events=[], host_work_link={}, messages=[])
    assert bind_work_link(session, VALID) is True
    assert bind_work_link(session, VALID) is False  # idempotent
    assert [row["type"] for row in session.events] == ["host/work_link"]
    assert derive_work_link(session.events) == VALID
    assert derive_messages(session.events) == []
    assert bind_work_link(session, {}) is True
    assert derive_work_link(session.events) == {}


def test_durable_run_exposes_only_safe_link_and_never_arbitrary_metadata(tmp_path: Path):
    store = DurableRunStore(tmp_path / "runs")
    run = store.create_run("session-1", metadata={"model_id": "model", "host_work_link": VALID, "secret": "SENSITIVE"})
    safe = store.public_record(run)
    assert safe["workLink"] == VALID
    assert "SENSITIVE" not in str(safe)
    assert "host_work_link" not in safe["metadata"]
    invalid = store.create_run("session-2", metadata={"host_work_link": {"taskId": "t", "secret": "hidden"}})
    assert "workLink" not in store.public_record(invalid)


def test_acp_session_new_and_restore_roundtrip_host_link_without_crew_patch(tmp_path: Path):
    # Some older ACP tests monkey-patch staticmethods on the class globally.
    # Keep this real ACP integration case in a clean interpreter, just as the
    # shipped harness runs in its own process.
    script = r"""
import json, os
from io import StringIO
from pathlib import Path
from kiro_byo.acp_server import AcpServer
from kiro_byo.session_events import derive_work_link
out = StringIO()
s = AcpServer(stdin=StringIO(), stdout=out)
base = os.environ["KIRO_BYO_HOME"]
valid = {"taskId":"crew-task.007", "assignmentId":"assign:worker-2", "role":"worker", "parentSessionId":"kiro-abc"}
def ask(n, method, params):
    s.handle({"jsonrpc":"2.0","id":n,"method":method,"params":params})
    return next(json.loads(x) for x in reversed(out.getvalue().splitlines()) if json.loads(x).get("id")==n)
try:
    initial = ask(1,"session/new",{"cwd":base,"_meta":{"kiro":{"byoWorkLink":valid}}})
    assert "result" in initial, initial
    sid = initial["result"]["sessionId"]
    assert initial["result"]["_meta"]["kiro"]["byoWorkLink"] == valid
    assert derive_work_link(s._load_persisted_record(sid)["events"]) == valid
    loaded = ask(2,"session/load",{"sessionId":sid,"cwd":base})
    assert "result" in loaded and loaded["result"]["_meta"]["kiro"]["byoWorkLink"] == valid
    new_link = {"taskId":"new-task","role":"worker"}
    updated = ask(3,"session/resume",{"sessionId":sid,"cwd":base,"_meta":{"kiro":{"byoWorkLink":new_link}}})
    assert "result" in updated, updated
    assert derive_work_link(s._load_persisted_record(sid)["events"]) == new_link
    cleared = ask(4,"session/resume",{"sessionId":sid,"cwd":base,"_meta":{"kiro":{"byoWorkLink":{}}}})
    assert "result" in cleared, cleared
    assert "byoWorkLink" not in cleared["result"]["_meta"]["kiro"]
    assert derive_work_link(s._load_persisted_record(sid)["events"]) == {}
    invalid = ask(5,"session/resume",{"sessionId":sid,"cwd":base,"_meta":{"kiro":{"byoWorkLink":{"role":"root"}}}})
    assert invalid["error"]["code"] == -32602
finally:
    s._executor.shutdown(wait=True)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        # The spawned ACP process must load this checkout even when the user
        # runs pytest directly from a source ZIP without ``pip install -e .``.
        # Keep any caller-provided PYTHONPATH after the checkout path.
        env={**os.environ, "KIRO_BYO_HOME": str(tmp_path),
             "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")
             + os.pathsep + os.environ.get("PYTHONPATH", "")},
        capture_output=True, text=True, timeout=35,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr


def test_work_link_is_recorded_in_real_run_without_changing_model_selection(tmp_path: Path):
    class Harness(TurnRunnerMixin):
        runtime_trace = None
        def trace(self, _message):
            pass

    runner = Harness()
    runner._kiro_durable_run_store = DurableRunStore(tmp_path / "runs")
    session = SimpleNamespace(
        session_id="session-3", events=[], host_work_link=VALID,
        active_agent_plan_id="", active_run_id="", active_run_step_id="", active_run_attempt_id="",
    )
    run_id = runner._begin_durable_run(session, None, selection="fixed-model", provider_id="p", model_id="m")
    assert run_id
    record = runner._kiro_durable_run_store.get(run_id)
    assert record["selection"] == "fixed-model"
    assert DurableRunStore.public_record(record)["workLink"] == VALID
    runner._finish_durable_run(session, outcome="success", provider_id="p", model_id="m")
