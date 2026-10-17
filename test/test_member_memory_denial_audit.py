"""Owner and routing refusals survive an unavailable security event log."""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from unittest import mock

import pytest
from member_memory_helpers import env as _member_env
from member_memory_helpers import request

from kiro_crew import sel as sel_module
from kiro_crew.dashboard.handlers import _shared, sessions

env = _member_env
pytestmark = [pytest.mark.asyncio, pytest.mark.xdist_group("member_memory_denial_audit")]


@pytest.mark.parametrize("audit_state", ["init_failure", "write_failure", "healthy"])
@pytest.mark.parametrize("verified_member", [False, True], ids=["missing_identity", "member"])
async def test_member_denial_survives_audit_failure_without_reading_history(
    env, monkeypatch, audit_state, verified_member
):
    loop_thread = threading.get_ident()
    initialization_threads = []
    audit_threads = []
    events = []

    def write(**event):
        audit_threads.append(threading.get_ident())
        events.append(event)
        if audit_state == "write_failure":
            raise OSError("security event log is unwritable")

    def event_log():
        initialization_threads.append(threading.get_ident())
        if audit_state == "init_failure":
            raise RuntimeError("SEL signing key is too short")
        return SimpleNamespace(log_api_access=write)

    monkeypatch.setattr(sel_module, "sel", event_log)
    if audit_state == "init_failure":
        await sel_module.warm_sel_singleton()
    list_history = mock.Mock(return_value=[{"key": "dashboard:owner", "title": "private"}])
    env.state.conversation_log.list_sessions = list_history
    if not verified_member:
        monkeypatch.setattr(
            "kiro_crew.execution_context.read_session_execution",
            mock.Mock(side_effect=ValueError("invalid execution record")),
        )

    response = await sessions.api_sessions(
        request(
            env,
            internal=True,
        )
    )

    assert response.status == (403 if verified_member else 409)
    assert json.loads(response.text) == (
        {
            "error": "This operation requires the owner. Use the member's scoped tools instead.",
            "code": "member_scope_denied",
        }
        if verified_member
        else {
            "error": "The execution identity is unavailable; Global memory was not used.",
            "code": "member_identity_unavailable",
        }
    )
    list_history.assert_not_called()
    assert len(initialization_threads) == (2 if audit_state == "init_failure" else 1)
    assert all(thread != loop_thread for thread in initialization_threads + audit_threads)
    if audit_state == "init_failure":
        assert events == []
    else:
        assert events == [
            {
                "caller": "internal",
                "operation": "api_sessions",
                "outcome": "denied",
                "source": "member_memory",
                "error": (
                    "Agent tools cannot use the owner's aggregate controls."
                    if verified_member
                    else "The execution identity is unavailable."
                ),
            }
        ]


async def test_owner_and_verified_scoped_member_keep_their_existing_admission(env, monkeypatch):
    event_log = mock.Mock(side_effect=RuntimeError("SEL signing key is unavailable"))
    monkeypatch.setattr(sel_module, "sel", event_log)
    list_history = mock.Mock(return_value=[])
    env.state.conversation_log.list_sessions = list_history

    response = await sessions.api_sessions(request(env, owner=True))
    assert response.status == 200
    assert json.loads(response.text) == {"sessions": [], "total": 0, "has_more": False}
    list_history.assert_called_once_with()
    scope, refusal = await _shared.internal_memory_scope(request(env, internal=True), "spawn.list")
    assert scope == "member-alice" and refusal is None
    event_log.assert_not_called()
