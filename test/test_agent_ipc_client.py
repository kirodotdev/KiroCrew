"""The client half of the agent socket: issuance on the array, redemption in the server.

``test_agent_ipc_enforced_spawn_e2e.py`` drives both halves through a real masked
spawn; these pin the rules each half applies on its own.
"""

from __future__ import annotations

import io
import os
import socket
import sys
import urllib.error
import urllib.request

import pytest

from kiro_crew import agent_ipc_client
from kiro_crew.acp import ipc_pickup
from kiro_crew.agent_ipc_wire import CAPABILITY_HEADER, GATEWAY_PID_ENV, PICKUP_ENV
from kiro_crew.dashboard import agent_ipc
from kiro_crew.mcp_gateway import STUB_MODULE
from kiro_crew.mcp_gateway.hashing import STUB_FLAGS_FLAG, encode_target_args
from kiro_crew.testing.ids import unallocatable_pids

# Pids no live process can own, so nothing here can reach a real process.
_ROOT_PID, _RESPAWNED_PID = unallocatable_pids(2)


class _Runtime:
    """Stands in for the runtime: the owner whose lifetime a pickup shares."""

    def __init__(self, pid: int | None = _ROOT_PID) -> None:
        self.pid = pid


def _array() -> list[dict]:
    return [
        {"name": "kirocrew-core", "command": "kirocrew", "args": ["mcp-core"], "env": []},
        {"name": "third-party", "command": "x", "args": [], "env": []},
    ]


@pytest.fixture
def bound(monkeypatch):
    store = agent_ipc.CapabilityStore()
    monkeypatch.setattr(agent_ipc, "STORE", store)
    monkeypatch.setattr(agent_ipc, "agent_socket_bound", lambda: True)
    return store


def _env(element: dict) -> dict[str, str]:
    return {pair["name"]: pair["value"] for pair in element.get("env") or []}


def _issue(array, backend="codex", token="tok", runtime=None):
    runtime = runtime or _Runtime()
    return ipc_pickup.with_ipc_pickups(
        array,
        backend=backend,
        session_token=token,
        runtime_pid=lambda: runtime.pid,
        owner=runtime,
    )


@pytest.mark.parametrize("backend", ["codex", "opencode", "goose", "pi"])
def test_an_enforced_harness_gets_a_pickup_on_kirocrew_core_only(bound, backend):
    runtime = _Runtime()
    core, other = _issue(_array(), backend=backend, runtime=runtime)
    env = _env(core)
    assert len(env[PICKUP_ENV]) == 64
    # The server checks the listener against this pid before it sends anything.
    assert env[GATEWAY_PID_ENV] == str(os.getpid())
    assert PICKUP_ENV not in _env(other) and GATEWAY_PID_ENV not in _env(other)
    # The pickup is bound to the session's token and the runtime's live root pid.
    entry = next(iter(bound._pickups.values()))
    assert entry.session_token == "tok" and entry.server == "kirocrew-core"
    assert entry.runtime_pid() == _ROOT_PID
    runtime.pid = _RESPAWNED_PID
    assert entry.runtime_pid() == _RESPAWNED_PID


@pytest.mark.parametrize(
    "args",
    [
        ["-m", STUB_MODULE, "--server", "kirocrew-core", "--agent", "kirocrew"],
        [
            "-m",
            STUB_MODULE,
            f"{STUB_FLAGS_FLAG}={encode_target_args(['--server', 'kirocrew-core'])}",
        ],
        ["-m", STUB_MODULE, f"{STUB_FLAGS_FLAG}=!not-base64!"],
    ],
    ids=["plain", "enveloped", "unreadable"],
)
def test_a_broker_stub_for_kirocrew_core_gets_no_pickup(bound, args):
    # A stub never takes the pickup out of its environment, so a same-uid shell in
    # the session could read it from /proc/<stub>/environ and redeem it.
    stub = {"name": "kirocrew-core", "command": sys.executable, "args": args, "env": []}
    (out,) = _issue([stub])
    assert PICKUP_ENV not in _env(out) and GATEWAY_PID_ENV not in _env(out)
    assert not bound._pickups


@pytest.mark.parametrize("backend", ["kiro", "kas", "claude"])
def test_a_harness_that_reads_the_secret_keeps_its_array_unchanged(bound, backend):
    array = _array()
    assert _issue(array, backend=backend) is array
    assert not bound._pickups


def test_no_pickup_without_an_agent_socket_or_a_token(bound, monkeypatch):
    array = _array()
    assert _issue(array, token="") is array
    monkeypatch.setattr(agent_ipc, "agent_socket_bound", lambda: False)
    assert _issue(array) is array
    assert not bound._pickups


def test_the_caller_array_is_not_mutated(bound):
    array = _array()
    _issue(array)
    assert array == _array()


def test_the_runtime_session_path_issues_the_pickup():
    """Sessions are built by the runtime's mirrored projection, so issuance is there."""
    import inspect

    from kiro_crew.acp import runtime_session_setup

    source = inspect.getsource(runtime_session_setup._mirrored_session_mcp)
    assert "with_ipc_pickups(" in source and "session_token=stub_token" in source


# --- the server side ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_client_state():
    agent_ipc_client._reset_for_tests()
    yield
    agent_ipc_client._reset_for_tests()


def _held(gateway_pid: int = 777) -> dict[str, str]:
    return {PICKUP_ENV: "p" * 64, GATEWAY_PID_ENV: str(gateway_pid)}


def test_take_pickup_removes_it_and_the_gateway_pid_from_the_environment():
    env = {**_held(), "OTHER": "1"}
    assert agent_ipc_client.take_pickup(env) is True
    assert PICKUP_ENV not in env and GATEWAY_PID_ENV not in env and env["OTHER"] == "1"
    assert agent_ipc_client.take_pickup({}) is False


def test_a_pickup_without_a_gateway_pid_is_not_kept():
    env = {PICKUP_ENV: "p" * 64}
    assert agent_ipc_client.take_pickup(env) is False
    assert PICKUP_ENV not in env and agent_ipc_client._pickup == ""


def test_no_pickup_means_no_capability_and_no_redemption(monkeypatch):
    called = []
    monkeypatch.setattr(agent_ipc_client, "unix_socket_urlopen", lambda *a, **k: called.append(a))
    assert agent_ipc_client.current_capability(lambda: 1) == ("", "")
    assert called == []


def test_a_transport_failure_keeps_the_pickup_for_the_next_request(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_ipc_client, "agent_socket_path", lambda port: tmp_path / "a.sock")
    agent_ipc_client.take_pickup(_held())

    def refuse(*_a, **_k):
        raise OSError("no gateway")

    monkeypatch.setattr(agent_ipc_client, "unix_socket_urlopen", refuse)
    assert agent_ipc_client.current_capability(lambda: 1) == ("", "")
    assert agent_ipc_client._pickup


def test_a_refused_pickup_is_dropped(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_ipc_client, "agent_socket_path", lambda port: tmp_path / "a.sock")
    agent_ipc_client.take_pickup(_held())

    def forbid(req, **_k):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr(agent_ipc_client, "unix_socket_urlopen", forbid)
    assert agent_ipc_client.current_capability(lambda: 1) == ("", "")
    assert agent_ipc_client._pickup == ""


@pytest.mark.skipif(sys.platform == "win32", reason="AF_UNIX transport")
def test_a_listener_that_is_not_the_gateway_receives_nothing(monkeypatch, short_sock_dir):
    """A process that rebinds the socket path gets neither the pickup nor a request."""
    path = short_sock_dir / "agent-1.sock"
    impostor = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    impostor.bind(str(path))
    impostor.listen(1)
    try:
        monkeypatch.setattr(agent_ipc_client, "agent_socket_path", lambda port: path)
        # The listener is this test process; the element names a different gateway pid.
        agent_ipc_client.take_pickup(_held(gateway_pid=os.getpid() + 1))
        assert agent_ipc_client.current_capability(lambda: 1) == ("", "")
        assert agent_ipc_client._pickup, "the pickup must be kept, unsent"
        impostor.settimeout(0.0)
        try:
            conn, _ = impostor.accept()
        except BlockingIOError:
            conn = None
        if conn is not None:
            conn.settimeout(0.0)
            try:
                received = conn.recv(4096)
            except BlockingIOError:
                received = b""
            conn.close()
            assert received == b"", "no request bytes may reach a listener that is not the gateway"
    finally:
        impostor.close()


def test_a_capability_request_carries_no_secret():
    req = urllib.request.Request(
        "http://127.0.0.1/api/x", headers={"X-Internal-Secret": "s", "X-Session-Key": "k"}
    )
    out = agent_ipc_client.capability_request(req, "c" * 64)
    headers = {k.lower(): v for k, v in out.header_items()}
    assert "x-internal-secret" not in headers
    assert headers[CAPABILITY_HEADER.lower()] == "c" * 64
    assert headers["x-session-key"] == "k"


def test_a_held_capability_sends_only_to_the_agent_socket(monkeypatch):
    from kiro_crew.mcp_core_parts import gateway

    sent = {}

    def fake_unix(req, timeout, *, socket_path, verify_peer=None):
        sent["socket"] = socket_path
        sent["checked"] = verify_peer is not None
        sent["headers"] = {k.lower() for k, _ in req.header_items()}
        return "response"

    def no_tcp(*_a, **_k):
        raise AssertionError("a capability-bearing request must not reach loopback_urlopen")

    monkeypatch.setattr(agent_ipc_client, "_capability", "c" * 64)
    monkeypatch.setattr(agent_ipc_client, "_socket", "/x/agent-ipc/agent-1.sock")
    monkeypatch.setattr(agent_ipc_client, "_gateway_pid", 777)
    monkeypatch.setattr(agent_ipc_client, "unix_socket_urlopen", fake_unix)
    # Through the facade: it is the patch surface every owner that binds the name reads.
    from kiro_crew import mcp_core

    monkeypatch.setattr(mcp_core, "loopback_urlopen", no_tcp)
    req = urllib.request.Request("http://127.0.0.1:1/api/x", headers={"X-Internal-Secret": "s"})
    assert gateway._api_urlopen(req, 5) == "response"
    assert sent["socket"] == "/x/agent-ipc/agent-1.sock"
    assert sent["checked"] is True
    assert "x-internal-secret" not in sent["headers"]
    assert CAPABILITY_HEADER.lower() in sent["headers"]


def test_the_session_comes_from_the_gateway_when_the_mapping_is_masked(monkeypatch):
    """Under the mask the trust root is hidden; a capability holder asks the gateway."""
    from kiro_crew import session_token_sig
    from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV

    monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, "tok")
    monkeypatch.setattr(session_token_sig, "verify_session_token", lambda tok: "")
    monkeypatch.setattr(agent_ipc_client, "capability_session_key", lambda: "chat-claimed")
    assert session_token_sig.session_key_from_env_token() == "chat-claimed"
    # A verifiable mapping still wins, and no token means no identity at all.
    monkeypatch.setattr(session_token_sig, "verify_session_token", lambda tok: "chat-mapped")
    assert session_token_sig.session_key_from_env_token() == "chat-mapped"
    monkeypatch.delenv(STUB_SESSION_TOKEN_ENV)
    assert session_token_sig.session_key_from_env_token() == ""


def test_no_capability_means_no_session_query(monkeypatch):
    monkeypatch.setattr(
        agent_ipc_client, "unix_socket_urlopen", lambda *a, **k: pytest.fail("no query expected")
    )
    assert agent_ipc_client.capability_session_key() == ""


def test_a_capability_refusal_reads_as_a_session_restart():
    from kiro_crew.mcp_core_parts import gateway

    exc = urllib.error.HTTPError(
        "http://127.0.0.1/api/x",
        403,
        "Forbidden",
        {},
        io.BytesIO(b'{"error": "Forbidden", "code": "ipc_capability_invalid"}'),
    )
    body = gateway._http_error_body(exc)
    assert body["code"] == "ipc_capability_invalid"
    assert "Start a new session" in body["error"]


def test_the_client_session_path_issues_the_pickup_too():
    """opencode and goose sessions are driven by an AcpClient, whose array is built here."""
    import inspect

    from kiro_crew.acp import client_mcp_array

    source = inspect.getsource(client_mcp_array._resolve_session_mcp_servers)
    assert source.count("_with_own_ipc_pickups(") == 2


def test_a_capability_holder_reads_its_tool_policy_by_capability(monkeypatch):
    """Under the mask the secret is empty; the exclusion must still be read and applied."""
    import json as _json

    from kiro_crew import mcp_shared, mcp_shared_policy

    sent = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            return _json.dumps({"exclude": ["artifact_save"]}).encode()

    def fake_open(req, timeout, *, capability, socket_path):
        sent["capability"] = capability
        sent["socket"] = socket_path
        return _Resp()

    monkeypatch.setattr(
        agent_ipc_client, "current_capability", lambda port_fn: ("c" * 64, "/x/agent.sock")
    )
    monkeypatch.setattr(agent_ipc_client, "open_with_capability", fake_open)
    monkeypatch.setattr(
        mcp_shared, "loopback_urlopen", lambda *a, **k: pytest.fail("the secret path was used")
    )
    mcp_shared_policy._excluded_tools_by_session.clear()
    policy = mcp_shared_policy._resolve_tool_policy("chat-1", ignore_negative_cache=True)
    mcp_shared_policy._excluded_tools_by_session.clear()
    assert sent == {"capability": "c" * 64, "socket": "/x/agent.sock"}
    assert "artifact_save" in policy.excluded
