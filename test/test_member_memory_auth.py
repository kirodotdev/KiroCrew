"""Ordinary authentication stays independent of canonical memory routing."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from member_memory_helpers import env as _member_env
from member_memory_helpers import request

from kiro_crew import member_memory_auth as auth
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers import _shared
from kiro_crew.execution_context import bind_session_execution, read_session_execution

env = _member_env


@pytest.mark.asyncio
async def test_internal_request_captures_one_member_before_other_work(env):
    req = request(env, internal=True)
    scope = await _shared.member_request_scope(req)
    assert scope.store == "member-alice"
    env.bind_session("dashboard:bob", "member-bob")
    bind_session_execution(
        "dashboard:alice", read_session_execution("dashboard:bob"), replace_existing=True
    )
    again = await _shared.member_request_scope(req)
    assert again is scope
    assert again.execution.member_id == "alice"


@pytest.mark.asyncio
async def test_untrusted_header_does_not_authenticate_memory_request(env):
    scope = await _shared.member_request_scope(request(env, internal=False))
    assert not scope.verified
    assert scope.execution is None


@pytest.mark.asyncio
async def test_missing_canonical_member_is_not_global(env):
    from kiro_crew.history import ConversationLog

    key = "dashboard:missing"
    ConversationLog().update_metadata(key, {"memory_store": "member-bob"})
    scope = await _shared.member_request_scope(request(env, session=key, internal=True))
    assert not scope.verified
    assert scope.store is None


@pytest.mark.asyncio
async def test_code_cron_uses_captured_execution_without_a_transcript(env):
    from kiro_crew.cron import CronJob
    from kiro_crew.execution_context import resolve_member_execution

    execution = resolve_member_execution(KiroCrewConfig.load(), "alice")
    job = CronJob(
        id="member-cron",
        name="synthetic",
        message="run",
        execution_context=execution.to_record(),
        member_id="alice",
        memory_store="member-alice",
        script="scripts/example.py:run",
    )
    env.state.crons = SimpleNamespace(_jobs=[job])

    scope = await _shared.member_request_scope(
        request(env, session="cron:member-cron", internal=True)
    )

    assert scope.verified
    assert scope.store == "member-alice"
    assert scope.execution == execution


@pytest.mark.asyncio
async def test_missing_code_cron_record_does_not_fall_back_to_global(env):
    from kiro_crew.cron import CronJob

    env.state.crons = SimpleNamespace(
        _jobs=[
            CronJob(
                id="broken-cron",
                name="synthetic",
                message="run",
                member_id="alice",
                memory_store="member-alice",
            )
        ]
    )

    scope = await _shared.member_request_scope(
        request(env, session="cron:broken-cron", internal=True)
    )

    assert not scope.verified
    assert scope.store is None


@pytest.mark.parametrize("peer", [None, False, 0])
def test_owner_bootstrap_requires_positive_peer_identity(monkeypatch, peer):
    monkeypatch.setattr(auth, "_request_peer_pid", lambda req: peer)
    monkeypatch.setattr(auth.platform_compat, "get_process_start_id", lambda pid: None)
    assert not auth.local_owner_bootstrap_allowed(SimpleNamespace())


@pytest.mark.parametrize(
    "host,backend,allowed", [(False, False, False), (True, False, True), (False, True, True)]
)
def test_owner_bootstrap_keeps_host_or_tracked_backend_permission(
    monkeypatch, host, backend, allowed
):
    monkeypatch.setattr(auth, "_request_peer_pid", lambda req: 123)
    monkeypatch.setattr(auth.platform_compat, "get_process_start_id", lambda pid: "start-id")
    monkeypatch.setattr(auth, "_verified_host_process", lambda pid: host)
    monkeypatch.setattr(auth, "_gateway_spawned_app_backend", lambda pid: backend)
    assert auth.local_owner_bootstrap_allowed(SimpleNamespace()) is allowed


def test_unknown_process_incarnation_never_bootstraps_owner(monkeypatch):
    monkeypatch.setattr(auth, "_request_peer_pid", lambda req: 123)
    monkeypatch.setattr(auth.platform_compat, "get_process_start_id", lambda pid: None)
    provenance = Mock(return_value=True)
    monkeypatch.setattr(auth, "_verified_host_process", provenance)
    assert not auth.local_owner_bootstrap_allowed(SimpleNamespace())
    provenance.assert_not_called()


@pytest.mark.parametrize(
    "platform,answer", [("linux", None), ("linux", False), ("darwin", None), ("darwin", True)]
)
def test_unknown_or_sandboxed_host_provenance_refuses_owner(monkeypatch, platform, answer):
    monkeypatch.setattr(auth.sys, "platform", platform)
    monkeypatch.setattr(auth.platform_compat, "process_namespaces_match", lambda *a: answer)
    monkeypatch.setattr(auth.platform_compat, "process_is_sandboxed", lambda *a: answer)
    assert not auth._verified_host_process(123)


@pytest.mark.parametrize("peer", ["match", "mismatch", "unverifiable"])
def test_owner_bootstrap_checks_unix_peer_before_process_provenance(monkeypatch, peer):
    from kiro_crew.dashboard import token_auth
    from kiro_crew.mcp_gateway import socketsec

    sock = object()
    monkeypatch.setattr(token_auth, "_unix_request_socket", lambda _request: sock)
    monkeypatch.setattr(
        socketsec, "check_peer_is_self", lambda _sock: socketsec.PeerCredResult(peer)
    )
    read_pid = Mock(return_value=123)
    provenance = Mock(return_value=True)
    monkeypatch.setattr(socketsec, "get_peer_pid", read_pid)
    monkeypatch.setattr(auth.platform_compat, "get_process_start_id", lambda _pid: "start-id")
    monkeypatch.setattr(auth, "_verified_host_process", provenance)

    assert auth.local_owner_bootstrap_allowed(SimpleNamespace()) is (peer == "match")
    if peer == "match":
        read_pid.assert_called_once_with(sock)
        provenance.assert_called_once_with(123)
    else:
        read_pid.assert_not_called()
        provenance.assert_not_called()


@pytest.mark.parametrize(
    "server,client",
    [
        (None, None),
        (("192.0.2.1", 1000), ("127.0.0.1", 2000)),
        (("127.0.0.1", 1000), ("192.0.2.2", 2000)),
    ],
)
def test_owner_bootstrap_requires_both_tcp_endpoints_to_be_loopback(monkeypatch, server, client):
    from kiro_crew.dashboard import token_auth

    monkeypatch.setattr(token_auth, "_unix_request_socket", lambda _request: None)
    resolve = Mock(return_value=123)
    monkeypatch.setattr(auth.platform_compat, "get_tcp_peer_pid", resolve)
    transport = SimpleNamespace(
        get_extra_info=lambda name: {"sockname": server, "peername": client}[name]
    )
    assert not auth.local_owner_bootstrap_allowed(SimpleNamespace(transport=transport))
    resolve.assert_not_called()


def test_owner_bootstrap_requires_transport_and_available_tcp_resolver(monkeypatch):
    from kiro_crew.dashboard import token_auth

    monkeypatch.setattr(token_auth, "_unix_request_socket", lambda _request: None)
    monkeypatch.setattr(auth.platform_compat, "get_tcp_peer_pid", None)
    assert not auth.local_owner_bootstrap_allowed(SimpleNamespace())
    transport = SimpleNamespace(get_extra_info=lambda _name: ("127.0.0.1", 1234))
    assert not auth.local_owner_bootstrap_allowed(SimpleNamespace(transport=transport))


@pytest.mark.parametrize("pid", [123, True, None])
def test_owner_bootstrap_uses_exact_tcp_pair_and_valid_process_identity(monkeypatch, pid):
    from kiro_crew.dashboard import token_auth

    monkeypatch.setattr(token_auth, "_unix_request_socket", lambda _request: None)
    server, client = ("::1", 1234, 0, 0), ("::1", 5678, 0, 0)
    transport = SimpleNamespace(
        get_extra_info=lambda name: {"sockname": server, "peername": client}[name]
    )
    resolve = Mock(return_value=pid)
    provenance = Mock(return_value=True)
    monkeypatch.setattr(auth.platform_compat, "get_tcp_peer_pid", resolve)
    monkeypatch.setattr(auth.platform_compat, "get_process_start_id", lambda _pid: "start-id")
    monkeypatch.setattr(auth, "_verified_host_process", provenance)
    assert auth.local_owner_bootstrap_allowed(SimpleNamespace(transport=transport)) is (pid == 123)
    resolve.assert_called_once_with(server[:2], client[:2])
    if pid == 123:
        provenance.assert_called_once_with(123)
    else:
        provenance.assert_not_called()


def test_owner_bootstrap_probe_exception_cannot_grant_permission(monkeypatch):
    monkeypatch.setattr(auth, "_request_peer_pid", Mock(side_effect=OSError("probe unavailable")))
    provenance = Mock(return_value=True)
    monkeypatch.setattr(auth, "_verified_host_process", provenance)
    assert not auth.local_owner_bootstrap_allowed(SimpleNamespace())
    provenance.assert_not_called()


@pytest.mark.parametrize("authenticated,key", [(False, "dashboard:alice"), (True, 42), (True, "")])
def test_request_identity_does_not_read_a_missing_or_untrusted_session(
    monkeypatch, authenticated, key
):
    req = SimpleNamespace(
        get=lambda name: authenticated if name == "internal_auth" else False,
        headers={"X-Session-Key": key},
    )
    read = Mock(side_effect=AssertionError("unexpected canonical read"))
    monkeypatch.setattr(auth, "read_session_execution", read)
    assert auth.memory_request_identity(req) == (None, False)
    read.assert_not_called()


@pytest.mark.parametrize("failure", [OSError("unreadable"), ValueError("malformed")])
def test_authenticated_session_identity_refuses_unreadable_canonical_record(monkeypatch, failure):
    req = SimpleNamespace(
        get=lambda name: True if name == "internal_auth" else True,
        headers={"X-Session-Key": "dashboard:alice", "X-Session-Token": "token"},
    )
    monkeypatch.setattr(auth, "verify_session_token", lambda _token: "dashboard:alice")
    read = Mock(side_effect=failure)
    monkeypatch.setattr(auth, "read_session_execution", read)
    assert auth.memory_request_identity(req) == (None, False)
    read.assert_called_once_with("dashboard:alice")


def test_tcp_request_requires_signed_session_token(monkeypatch):
    req = SimpleNamespace(
        get=lambda name: True if name == "internal_auth" else False,
        headers={"X-Session-Key": "dashboard:alice", "X-Session-Token": "forged"},
    )
    monkeypatch.setattr(auth, "verify_session_token", lambda _token: "dashboard:bob")
    read = Mock()
    monkeypatch.setattr(auth, "read_session_execution", read)
    assert auth.memory_request_identity(req) == (None, False)
    read.assert_not_called()


def test_verified_unix_peer_can_authenticate_without_token(monkeypatch):
    req = SimpleNamespace(
        get=lambda name: {"internal_auth": True, "peer_verified": True}.get(name, False),
        headers={"X-Session-Key": "dashboard:alice"},
    )
    monkeypatch.setattr(auth, "read_session_execution", Mock(return_value=object()))
    assert auth.memory_request_identity(req) == ("dashboard:alice", True)
