"""Phase 3: explicit Crew host correlation for reused and pooled ACP sessions."""
from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from types import MethodType
from unittest.mock import patch

import pytest

from kiro_byo.acp_server import AcpServer, Session, SessionBusyError
from kiro_byo.session_events import derive_work_link, derive_messages

WORK_A = {"taskId": "crew.7", "assignmentId": "assign.1", "role": "worker", "parentSessionId": "mate.1"}
WORK_B = {"taskId": "crew.8", "assignmentId": "assign.2", "role": "worker", "parentSessionId": "mate.1"}


def request(server, out, seq, method, params):
    server.handle({"jsonrpc": "2.0", "id": seq, "method": method, "params": params})
    return next(json.loads(row) for row in reversed(out.getvalue().splitlines()) if json.loads(row).get("id") == seq)


@pytest.fixture
def harness(tmp_path):
    with patch("kiro_byo.acp_server.home_dir", return_value=tmp_path / "home"):
        output = StringIO()
        server = AcpServer(stdin=StringIO(), stdout=output)
        session = Session("crew-session", str(tmp_path), "kiro-byo/echo")
        server._persist_session(session)
        server._put_session(session)
        try:
            yield server, session, output
        finally:
            server._executor.shutdown(wait=True)


def test_late_bind_persists_and_is_idempotent(harness):
    server, session, out = harness
    first = request(server, out, 1, "_kiro/work/bind", {"sessionId": session.session_id, "workLink": WORK_A})
    assert first["result"] == {"sessionId": session.session_id, "workLink": WORK_A, "changed": True, "scope": "diagnostic_only"}
    revision = session.storage_revision
    second = request(server, out, 2, "_kiro/work/bind", {"sessionId": session.session_id, "workLink": WORK_A})
    assert second["result"]["changed"] is False
    assert session.storage_revision == revision
    assert derive_work_link(server._load_persisted_record(session.session_id)["events"]) == WORK_A
    assert derive_messages(session.events) == []
    detached = request(server, out, 3, "_kiro/work/bind", {"sessionId": session.session_id, "workLink": {}})
    assert detached["result"]["changed"] is True
    assert derive_work_link(server._load_persisted_record(session.session_id)["events"]) == {}


def test_late_bind_does_not_change_running_session(harness):
    server, session, out = harness
    assert request(server, out, 1, "_kiro/work/bind", {"sessionId": session.session_id, "workLink": WORK_A}).get("result")
    revision = session.storage_revision
    session.turn_lock.acquire()
    try:
        denied = request(server, out, 2, "_kiro/work/bind", {"sessionId": session.session_id, "workLink": WORK_B})
        assert denied["error"]["code"] == -32002
        assert session.storage_revision == revision
        assert session.host_work_link == WORK_A
    finally:
        session.turn_lock.release()


@pytest.mark.parametrize("value", [None, [], {"taskId": "../path"}, {"taskId": "safe", "permission": "allow"}, {"role": "root"}])
def test_late_bind_rejects_invalid_identity(harness, value):
    server, session, out = harness
    denied = request(server, out, 1, "_kiro/work/bind", {"sessionId": session.session_id, "workLink": value})
    assert denied["error"]["code"] == -32602
    assert session.storage_revision == 1


def test_late_bind_reverts_memory_on_persistence_failure(harness, monkeypatch):
    server, session, out = harness
    original = list(session.events)
    monkeypatch.setattr(server, "_persist_session", lambda _: False)
    result = request(server, out, 1, "_kiro/work/bind", {"sessionId": session.session_id, "workLink": WORK_A})
    assert result["error"]["code"] == -32002
    assert session.events == original
    assert session.host_work_link == {}


def test_prompt_level_binding_survives_even_if_session_is_reused(harness):
    server, session, out = harness
    observed = []

    def simulate_turn(self, req_id, params, current, sid, started, run_id, strategy):
        observed.append((dict(current.host_work_link), current.model))
        self.result(req_id, {"stopReason": "end_turn"})

    server._execute_prompt_request_locked = MethodType(simulate_turn, server)
    prompt = lambda seq, link: request(server, out, seq, "session/prompt", {
        "sessionId": session.session_id,
        "prompt": [{"type": "text", "text": "plain prompt, not a task identifier"}],
        "_meta": {"kiro": {"byoWorkLink": link}},
    })
    assert prompt(1, WORK_A)["result"]["stopReason"] == "end_turn"
    assert prompt(2, WORK_B)["result"]["stopReason"] == "end_turn"
    assert observed == [(WORK_A, "kiro-byo/echo"), (WORK_B, "kiro-byo/echo")]
    assert derive_work_link(server._load_persisted_record(session.session_id)["events"]) == WORK_B
    assert len([e for e in session.events if e.get("type") == "host/work_link"]) == 2


def test_prompt_with_bad_link_never_calls_model(harness):
    server, session, out = harness
    def no_turn(*args, **kwargs):
        raise AssertionError("the model must never run")
    server._execute_prompt_request_locked = no_turn
    result = request(server, out, 1, "session/prompt", {
        "sessionId": session.session_id,
        "prompt": [{"type": "text", "text": "malformed"}],
        "_meta": {"kiro": {"byoWorkLink": {"taskId": "ok", "capability": "all"}}},
    })
    assert result["error"]["code"] == -32602
    assert session.storage_revision == 1
    assert session.host_work_link == {}


def test_prompt_bind_failure_releases_turn_lock(harness, monkeypatch):
    server, session, out = harness
    monkeypatch.setattr(server, "_persist_session", lambda _: False)
    result = request(server, out, 1, "session/prompt", {
        "sessionId": session.session_id,
        "prompt": [{"type": "text", "text": "x"}],
        "_meta": {"kiro": {"byoWorkLink": WORK_A}},
    })
    assert result["error"]["code"] == -32002
    assert not session.turn_lock.locked()
    assert session.host_work_link == {}
    assert session.active_turn is False


def test_binding_never_accepts_unidentified_session(harness):
    server, session, out = harness
    result = request(server, out, 1, "_kiro/work/bind", {"sessionId": "does-not-exist", "workLink": WORK_A})
    assert result["error"]["code"] == -32602


def test_two_prompt_bindings_create_distinct_run_lineage(harness, tmp_path):
    """Real durable Run metadata is frozen at each turn, not retroactively re-labelled."""
    from kiro_byo.durable_runs import DurableRunStore
    from kiro_byo.work_trace import work_trace_snapshot
    server, session, out = harness
    store = DurableRunStore(tmp_path / "run-store")
    server._kiro_durable_run_store = store
    saved_run_ids = []

    def create_run(self, req_id, params, current, sid, started, run_id, strategy):
        new_id = self._begin_durable_run(current, None, selection="kiro-byo/echo", provider_id="local", model_id="echo")
        assert new_id
        saved_run_ids.append(new_id)
        self._finish_durable_run(current, outcome="success", provider_id="local", model_id="echo")
        self.result(req_id, {"stopReason": "end_turn"})

    server._execute_prompt_request_locked = MethodType(create_run, server)
    for index, link in enumerate((WORK_A, WORK_B), start=1):
        response = request(server, out, index, "session/prompt", {
            "sessionId": session.session_id,
            "prompt": [{"type": "text", "text": "do work"}],
            "_meta": {"kiro": {"byoWorkLink": link}},
        })
        assert "result" in response, response
    assert len(saved_run_ids) == 2
    first, second = (store.get(run_id) for run_id in saved_run_ids)
    assert first["metadata"]["host_work_link"] == WORK_A
    assert second["metadata"]["host_work_link"] == WORK_B
    view = work_trace_snapshot(store=store)
    assert {group["taskId"] for group in view["tasks"]} == {"crew.7", "crew.8"}
    assert {group["runCount"] for group in view["tasks"]} == {1}


def test_legacy_prompt_does_not_change_explicit_work_link(harness):
    server, session, out = harness
    seen = []
    def fake_turn(self, req_id, params, current, *args):
        seen.append(dict(current.host_work_link))
        self.result(req_id, {"stopReason": "end_turn"})
    server._execute_prompt_request_locked = MethodType(fake_turn, server)
    request(server, out, 1, "_kiro/work/bind", {"sessionId": session.session_id, "workLink": WORK_A})
    response = request(server, out, 2, "session/prompt", {
        "sessionId": session.session_id,
        "prompt": [{"type": "text", "text": "legacy"}],
    })
    assert "result" in response and seen == [WORK_A]
    assert derive_work_link(server._load_persisted_record(session.session_id)["events"]) == WORK_A


def test_initialize_advertises_opt_in_correlation_without_claiming_orchestration():
    from kiro_byo.protocol import initialize_result
    caps = initialize_result()["agentCapabilities"]["_meta"]["kiro"]
    assert caps["workCorrelation"] == {
        "version": 1,
        "bindMethod": "_kiro/work/bind",
        "promptMetadata": "_meta.kiro.byoWorkLink",
        "scope": "diagnostic_only",
    }
    assert "_kiro/team/spawn" not in caps["extensionMethods"]
    assert "_kiro/work/bind" not in caps["extensionMethods"]  # separate opt-in semantic contract
