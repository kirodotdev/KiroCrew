"""Private authorization refusals survive an unavailable security event log."""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from unittest import mock

import pytest
from member_memory_helpers import env as _member_env
from member_memory_helpers import member_proof as _member_proof
from member_memory_helpers import request

from kiro_crew import sel as sel_module
from kiro_crew.dashboard.handlers import _shared, sessions

env = _member_env
member_proof = _member_proof
pytestmark = [pytest.mark.asyncio, pytest.mark.xdist_group("member_memory_denial_audit")]


@pytest.mark.parametrize("audit_state", ["init_failure", "write_failure", "healthy"])
@pytest.mark.parametrize("verified_private", [False, True], ids=["unverified", "private"])
async def test_private_denial_survives_audit_failure_without_reading_history(
    env, member_proof, monkeypatch, audit_state, verified_private
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

    response = await sessions.api_sessions(
        request(
            env,
            internal=True,
            proof=member_proof if verified_private else member_proof + "tampered",
        )
    )

    assert response.status == 403
    assert json.loads(response.text) == (
        {
            "error": "This operation requires the owner. Use the member's scoped tools instead.",
            "code": "member_scope_denied",
        }
        if verified_private
        else {
            "error": "The caller's member session could not be verified.",
            "code": "member_session_unverified",
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
                    "A private member cannot use the owner's aggregate controls."
                    if verified_private
                    else "The caller's protected session could not be verified."
                ),
            }
        ]


async def test_owner_and_verified_scoped_member_keep_their_existing_admission(
    env, member_proof, monkeypatch
):
    event_log = mock.Mock(side_effect=RuntimeError("SEL signing key is unavailable"))
    monkeypatch.setattr(sel_module, "sel", event_log)
    list_history = mock.Mock(return_value=[])
    env.state.conversation_log.list_sessions = list_history

    response = await sessions.api_sessions(request(env, owner=True))
    assert response.status == 200
    assert json.loads(response.text) == {"sessions": [], "total": 0, "has_more": False}
    list_history.assert_called_once_with()
    scope, refusal = await _shared.internal_memory_scope(
        request(env, internal=True, proof=member_proof), "spawn.list"
    )
    assert scope == "member-alice" and refusal is None
    event_log.assert_not_called()
