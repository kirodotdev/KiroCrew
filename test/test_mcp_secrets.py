"""Unit tests for the sandboxed ``kirocrew-secrets`` MCP forwarder.

The forwarder holds NO vault access: it POSTs non-secret request intent to the
host ``/api/mediated-secret-request`` endpoint over loopback and returns only
the sanitized response. These tests exercise the schema, session-key
resolution, the per-call member-proof forwarding, the happy path, and every
refusal branch — all with the network + config seams mocked, so no real
loopback or vault is touched.
"""

from __future__ import annotations

import json
import urllib.error
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew import mcp_secrets


@pytest.fixture(autouse=True)
def _force_unix_platform(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the non-Windows path for the whole module.

    ``_call_tool_inner`` returns an explicit unsupported-platform refusal on
    Windows BEFORE touching the forwarder, so on a Windows CI runner every test
    that exercises the header-count guard, session-key resolution, the loopback
    forwarder, ``verify_peer``, or a specific error message would otherwise get
    the platform refusal instead of the behavior it asserts. Pinning IS_WINDOWS
    False makes those tests exercise the real mediation path on any runner OS.
    The dedicated ``test_windows_returns_explicit_unsupported_platform_result``
    sets IS_WINDOWS True itself, and that later monkeypatch wins.

    Patches through ``_floor_monkeypatch`` (the independent fixture-only stack),
    never the shared ``monkeypatch``: a test that calls ``monkeypatch.undo()``
    mid-body must not also drop this module-wide platform pin.
    """
    _floor_monkeypatch.setattr(mcp_secrets.platform_compat, "IS_WINDOWS", False)


class _FakeResp:
    """Context-manager stand-in for the loopback response object."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._raw = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def read(self) -> bytes:
        return self._raw


def _patch_transport(
    monkeypatch: pytest.MonkeyPatch,
    *,
    urlopen: Any,
    secret: str = "internal-secret",
) -> list[Any]:
    """Mock config + loopback so _call_tool_inner never touches a real host.

    Returns a list the fake urlopen appends the outgoing Request to, so a test
    can assert on the headers the forwarder built.
    """
    captured: list[Any] = []

    monkeypatch.setattr(mcp_secrets, "current_tool_call_id", lambda: "test-call-1", raising=False)

    monkeypatch.setattr(
        mcp_secrets.KiroCrewConfig,
        "load",
        classmethod(
            lambda cls: SimpleNamespace(dashboard=SimpleNamespace(url="http://127.0.0.1:8080"))
        ),
    )
    monkeypatch.setattr(mcp_secrets, "parse_dashboard_url", lambda _url: ("127.0.0.1", 8080))
    monkeypatch.setattr(mcp_secrets, "read_local_secret", lambda _port: secret)
    monkeypatch.setattr(mcp_secrets, "dashboard_socket_path", lambda _port: "/tmp/nonexistent.sock")

    def _urlopen(
        req: Any, timeout: int = 0, *, socket_path: Any = None, verify_peer: Any = None
    ) -> Any:
        # The claim round-trip precedes the mediated request; answer it locally
        # so the double under test only sees the dispatch call.
        if str(getattr(req, "full_url", "")).endswith("/api/mediated-secret-capability"):
            return _FakeResp({"capability": "cap-token"})
        captured.append(req)
        return urlopen(req, timeout)

    monkeypatch.setattr(mcp_secrets, "unix_socket_urlopen", _urlopen)
    return captured


def test_list_tools_advertises_the_single_tool_and_schema() -> None:
    tools = mcp_secrets._list_tools()
    assert [t["name"] for t in tools] == ["call_api_with_secret"]
    schema = tools[0]["inputSchema"]
    assert schema["required"] == ["secret_name", "method", "url"]
    assert schema["additionalProperties"] is False


def test_validate_args_accepts_a_well_formed_call() -> None:
    args = mcp_secrets._validate_args(
        "call_api_with_secret",
        {"secret_name": "STRIPE", "method": "GET", "url": "https://api.example.com/v1"},
    )
    assert args["secret_name"] == "STRIPE"


def test_validate_args_rejects_a_bad_method() -> None:
    with pytest.raises(Exception):
        mcp_secrets._validate_args(
            "call_api_with_secret",
            {"secret_name": "K", "method": "TRACE", "url": "https://api.example.com"},
        )


def test_oversized_collections_are_refused_before_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """A headers/query map past the item ceiling, or a payload past the byte
    ceiling, is refused in-tool before any copy/serialize/dispatch — bounding the
    MCP worker's memory. Fails if the count/size guards are removed."""
    monkeypatch.setattr(mcp_secrets, "_resolve_session_key", lambda: "member:alice")
    dispatched: list[Any] = []

    def _urlopen(
        req: Any, timeout: int = 0, *, socket_path: Any = None, verify_peer: Any = None
    ) -> Any:
        dispatched.append(req)  # pragma: no cover - must not run on an oversize call
        return _FakeResp({"status": 200, "headers": {}, "body": "", "truncated": False})

    monkeypatch.setattr(mcp_secrets, "unix_socket_urlopen", _urlopen)
    monkeypatch.setattr(
        mcp_secrets.KiroCrewConfig,
        "load",
        classmethod(
            lambda cls: SimpleNamespace(dashboard=SimpleNamespace(url="http://127.0.0.1:8080"))
        ),
    )
    monkeypatch.setattr(mcp_secrets, "parse_dashboard_url", lambda _url: ("127.0.0.1", 8080))
    monkeypatch.setattr(mcp_secrets, "read_local_secret", lambda _port: "s")
    monkeypatch.setattr(mcp_secrets, "dashboard_socket_path", lambda _port: "/tmp/x.sock")

    too_many = {f"H{i}": "v" for i in range(mcp_secrets._MAX_MAP_ITEMS + 1)}
    out = mcp_secrets._call_tool_inner(
        "call_api_with_secret",
        {
            "secret_name": "K",
            "method": "GET",
            "url": "https://api.example.com",
            "headers": too_many,
        },
    )
    assert "too many headers" in out.lower()
    assert not dispatched, "an oversized call must never reach the transport"


def test_windows_returns_explicit_unsupported_platform_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a platform without AF_UNIX (Windows) the tool must fail closed with an
    EXPLICIT unsupported-platform message BEFORE any transport, not the generic
    'could not be completed' the broad handler would return once
    unix_socket_urlopen raised a bare OSError. The mediated transport is
    credential-safe only via the AF_UNIX + SO_PEERCRED channel, which Windows
    lacks, so refusing here is the documented supported behavior.

    Mutation guard: removing the `hasattr(socket, "AF_UNIX")` guard makes the
    call fall through to the transport and this message assertion fails.
    """
    dispatched: list[Any] = []

    def _urlopen(*a: Any, **k: Any) -> Any:
        dispatched.append(a)  # pragma: no cover - must not run without AF_UNIX
        raise AssertionError("transport must not be attempted without AF_UNIX")

    monkeypatch.setattr(mcp_secrets, "unix_socket_urlopen", _urlopen)
    monkeypatch.setattr(mcp_secrets, "_resolve_session_key", lambda: "member:alice")
    # Simulate Windows via the real discriminator. hasattr(socket, "AF_UNIX") is
    # NOT reliable: modern Windows Python exposes AF_UNIX yet the dashboard starts
    # no Unix site and there is no SO_PEERCRED channel, so the gate is IS_WINDOWS.
    monkeypatch.setattr(mcp_secrets.platform_compat, "IS_WINDOWS", True)

    out = mcp_secrets._call_tool_inner(
        "call_api_with_secret",
        {"secret_name": "K", "method": "GET", "url": "https://api.example.com"},
    )
    assert "not supported on this platform" in out.lower()
    assert "could not be completed" not in out.lower()
    assert not dispatched, "no transport attempt on an unsupported platform"


def test_resolve_session_key_prefers_the_protected_member_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_secrets, "protected_member_session_for_pid", lambda _pid: "member-sess")
    assert mcp_secrets._resolve_session_key() == "member-sess"


def test_resolve_session_key_prefers_the_verified_caller_meta_over_pid_and_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # On the gateway-proxied/pooled path the caller-meta session key is the
    # authoritative one; the pool worker's PID/env key would mismatch the
    # gateway-injected proof and 403. The caller-meta key must win.
    monkeypatch.setattr(
        mcp_secrets, "current_caller", lambda: SimpleNamespace(session_key="caller-sess")
    )
    monkeypatch.setattr(mcp_secrets, "protected_member_session_for_pid", lambda _pid: "pid-sess")
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "env-sess")
    assert mcp_secrets._resolve_session_key() == "caller-sess"


def test_resolve_session_key_falls_back_to_the_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # A genuinely ABSENT binding (protected is None) is the only case that falls
    # through to the managed-MCP-subprocess env key.
    monkeypatch.setattr(mcp_secrets, "current_caller", lambda: None)
    monkeypatch.setattr(mcp_secrets, "protected_member_session_for_pid", lambda _pid: None)
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "env-sess")
    assert mcp_secrets._resolve_session_key() == "env-sess"


def test_resolve_session_key_revoked_record_is_a_hard_refusal_not_env_fallthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A revoked/invalid protected record returns "" (non-None STOPS the ladder).
    # It must NOT fall through to the ambient KIROCREW_SESSION_KEY: that env key,
    # the signed token, and the pid mapping are all writable by the same uid the
    # binding fences, so a fall-through would let a fenced process re-identify as
    # the ambient session on the credential-egress path. The canonical contract
    # (member_memory_auth.protected_member_session_for_pid) is that a blank is a
    # hard refusal the host endpoint 403s.
    monkeypatch.setattr(mcp_secrets, "current_caller", lambda: None)
    monkeypatch.setattr(mcp_secrets, "protected_member_session_for_pid", lambda _pid: "")
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "env-sess")
    assert mcp_secrets._resolve_session_key() == "", (
        "a revoked protected record must be a hard refusal, not a fall-through to "
        "the ambient env key"
    )


def test_resolve_session_key_raising_probe_is_a_hard_refusal_not_env_fallthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A RAISING probe (e.g. an induced-unreadable binding, a Seatbelt deny) is a
    # hard refusal too: the same-uid fence means an unreadable binding must never
    # be re-read as identity from the ambient env key. Even with KIROCREW_SESSION_KEY
    # SET, the result is "" — the exception is not swallowed into a fall-through.
    def _raise(_pid: int) -> str:
        raise RuntimeError("induced-unreadable protected binding")

    monkeypatch.setattr(mcp_secrets, "current_caller", lambda: None)
    monkeypatch.setattr(mcp_secrets, "protected_member_session_for_pid", _raise)
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "env-sess")
    assert mcp_secrets._resolve_session_key() == "", (
        "a raising protected probe must refuse hard, not fall through to the " "ambient env key"
    )


def test_resolve_session_key_is_empty_when_nothing_identifies_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(_pid: int) -> str:
        raise RuntimeError("no protected topology")

    monkeypatch.setattr(mcp_secrets, "protected_member_session_for_pid", _raise)
    monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
    assert mcp_secrets._resolve_session_key() == ""


def test_call_tool_inner_rejects_an_unknown_tool() -> None:
    assert mcp_secrets._call_tool_inner("nope", {}).startswith("Error: Unknown tool")


def test_call_tool_inner_refuses_without_a_session_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_secrets, "_resolve_session_key", lambda: "")
    out = mcp_secrets._call_tool_inner(
        "call_api_with_secret",
        {"secret_name": "K", "method": "GET", "url": "https://api.example.com"},
    )
    assert "could not be identified" in out


def test_happy_path_returns_only_sanitized_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_secrets, "_resolve_session_key", lambda: "sess")
    # No caller in context -> no proof header (endpoint would 403, but the
    # forwarder path is what we cover here).
    monkeypatch.setattr(mcp_secrets, "current_caller", lambda: None, raising=False)

    def _ok(_req: Any, _timeout: int) -> _FakeResp:
        return _FakeResp(
            {
                "status": 200,
                "headers": {"content-type": "application/json"},
                "body": '{"ok":true}',
                "truncated": False,
                "final_url_origin": "https://api.example.com",
                "leaked": "must-not-appear",
            }
        )

    _patch_transport(monkeypatch, urlopen=_ok)
    out = mcp_secrets._call_tool_inner(
        "call_api_with_secret",
        {"secret_name": "K", "method": "GET", "url": "https://api.example.com/v1"},
    )
    parsed = json.loads(out)
    assert parsed["status"] == 200
    assert parsed["origin"] == "https://api.example.com"
    # Only allowlisted fields are surfaced; an unexpected field is dropped.
    assert "leaked" not in parsed


def test_forwards_the_session_key_and_no_proof_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller forwards its attested X-Session-Key and the loopback secret,
    and sends NO client-minted proof header — the host endpoint authenticates
    the request itself via member_request_scope (the transport attestation),
    so the trust never rests on a header this sandboxed process supplies."""
    monkeypatch.setattr(mcp_secrets, "_resolve_session_key", lambda: "member:alice")

    def _ok(_req: Any, _timeout: int = 0, *, socket_path: Any = None) -> _FakeResp:
        return _FakeResp({"status": 200, "headers": {}, "body": "", "truncated": False})

    captured = _patch_transport(monkeypatch, urlopen=_ok)
    monkeypatch.setattr(mcp_secrets, "current_caller", lambda: None)

    mcp_secrets._call_tool_inner(
        "call_api_with_secret",
        {"secret_name": "K", "method": "GET", "url": "https://api.example.com"},
    )
    assert captured, "the forwarder should have issued a loopback request"
    sent = captured[0].headers
    # The attested session key is forwarded (this is what the endpoint verifies).
    assert sent.get("X-session-key") == "member:alice"
    # NO member-session proof header of any spelling is present.
    assert not any("proof" in k.lower() for k in sent)


def test_forwards_the_signed_session_token_on_the_pooled_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a POOLED backend the peer ancestry does not resolve, so the endpoint's
    ``session_key_is_attested`` needs the signed per-session token as its second
    channel. The caller must attach it (from a gateway-forwarded caller block),
    exactly as every other control-plane server does — without it, every pooled
    mediated call is refused. Fails if the token header is dropped."""
    from types import SimpleNamespace

    monkeypatch.setattr(mcp_secrets, "_resolve_session_key", lambda: "member:alice")
    # A gateway-forwarded caller block carries the signed token on the pooled path.
    monkeypatch.setattr(
        mcp_secrets,
        "current_caller",
        lambda: SimpleNamespace(session_token="signed.tok.value", from_gateway=True),
    )

    def _ok(_req: Any, _timeout: int = 0, *, socket_path: Any = None) -> _FakeResp:
        return _FakeResp({"status": 200, "headers": {}, "body": "", "truncated": False})

    captured = _patch_transport(monkeypatch, urlopen=_ok)
    mcp_secrets._call_tool_inner(
        "call_api_with_secret",
        {"secret_name": "K", "method": "GET", "url": "https://api.example.com"},
    )
    assert captured, "the forwarder should have issued a loopback request"
    sent = {k.lower(): v for k, v in captured[0].headers.items()}
    # The signed session token rides along under its canonical header spelling.
    assert sent.get("x-session-token") == "signed.tok.value", sent


def test_refuses_a_rebound_dashboard_socket_before_sending(monkeypatch: pytest.MonkeyPatch) -> None:
    """The forwarder passes a verify_peer that authenticates the answering peer
    as the SPECIFIC gateway the run-marker records — its pid AND a matching
    start-token — not merely as some ancestor. The untrusted ACP harness is this
    process's parent (an ancestor), so an ancestor check would admit a harness
    that rebound the socket; the published-pid check refuses it. A pid mismatch, a
    recycled pid (start-token mismatch), an absent record, and an unreadable peer
    all raise before any bytes go out — closing the socket-rebind capture path."""
    monkeypatch.setattr(mcp_secrets, "_resolve_session_key", lambda: "member:alice")
    monkeypatch.setattr(mcp_secrets, "current_caller", lambda: None)
    monkeypatch.setattr(mcp_secrets, "current_tool_call_id", lambda: "test-call-1", raising=False)
    monkeypatch.setattr(
        mcp_secrets.KiroCrewConfig,
        "load",
        classmethod(
            lambda cls: SimpleNamespace(dashboard=SimpleNamespace(url="http://127.0.0.1:8080"))
        ),
    )
    monkeypatch.setattr(mcp_secrets, "parse_dashboard_url", lambda _url: ("127.0.0.1", 8080))
    monkeypatch.setattr(mcp_secrets, "read_local_secret", lambda _port: "internal-secret")
    monkeypatch.setattr(mcp_secrets, "dashboard_socket_path", lambda _port: "/tmp/nonexistent.sock")

    captured_verifier: list[Any] = []

    def _urlopen(
        req: Any, timeout: int = 0, *, socket_path: Any = None, verify_peer: Any = None
    ) -> Any:
        captured_verifier.append(verify_peer)
        if str(getattr(req, "full_url", "")).endswith("/api/mediated-secret-capability"):
            return _FakeResp({"capability": "cap-token"})
        return _FakeResp({"status": 200, "headers": {}, "body": "", "truncated": False})

    monkeypatch.setattr(mcp_secrets, "unix_socket_urlopen", _urlopen)
    # The gateway published pid 42 with start-token "start-42" to the owner-only
    # run-marker; pid 42's live start-token still reads "start-42".
    monkeypatch.setattr(
        mcp_secrets.run_marker, "pid_path", lambda _port: "/tmp/gw.pid", raising=False
    )
    monkeypatch.setattr(
        mcp_secrets.run_marker,
        "read_pid_record_path",
        lambda _path: (42, "start-42"),
        raising=False,
    )
    monkeypatch.setattr(
        mcp_secrets.run_marker,
        "pid_start_token",
        lambda pid: "start-42" if pid == 42 else "start-other",
        raising=False,
    )
    monkeypatch.setattr(mcp_secrets, "get_peer_pid", lambda _s: 42)
    # The kernel-attested peer-principal check is the new first gate: patch it to
    # MATCH for the genuine-gateway path (a bare object() has no real socket, so
    # the real check would return UNVERIFIABLE). The dedicated test below drives
    # the MISMATCH/UNVERIFIABLE refusal.
    monkeypatch.setattr(
        mcp_secrets, "check_peer_is_self", lambda _s: mcp_secrets.PeerCredResult.MATCH
    )

    mcp_secrets._call_tool_inner(
        "call_api_with_secret",
        {"secret_name": "K", "method": "GET", "url": "https://api.example.com"},
    )
    assert (
        captured_verifier and captured_verifier[0] is not None
    ), "the forwarder must pass a verify_peer to the socket"
    verify = captured_verifier[0]
    # The genuine gateway (confirmed principal, published pid 42, matching
    # start-token) is accepted.
    verify(object())
    # The run-marker is agent-writable, so the kernel-attested peer-principal
    # check is the anchor that does not depend on it: a peer whose principal is
    # NOT confirmed to be ours is refused even if it rewrote the run-marker to
    # record its own (now peer-matching) pid + start-token.
    monkeypatch.setattr(
        mcp_secrets.run_marker, "pid_start_token", lambda _pid: "start-42", raising=False
    )
    monkeypatch.setattr(
        mcp_secrets, "check_peer_is_self", lambda _s: mcp_secrets.PeerCredResult.MISMATCH
    )
    with pytest.raises(PermissionError):
        verify(object())
    monkeypatch.setattr(
        mcp_secrets, "check_peer_is_self", lambda _s: mcp_secrets.PeerCredResult.UNVERIFIABLE
    )
    with pytest.raises(PermissionError):
        verify(object())
    # Restore MATCH so the remaining run-marker refusal assertions exercise their
    # own branches rather than tripping the principal gate first.
    monkeypatch.setattr(
        mcp_secrets, "check_peer_is_self", lambda _s: mcp_secrets.PeerCredResult.MATCH
    )
    # F1: an ANCESTOR that is not the published gateway pid — a rebinding harness,
    # the untrusted parent of this process — is refused. The old ancestor walk
    # would have accepted it; the published-pid check does not.
    monkeypatch.setattr(mcp_secrets, "get_peer_pid", lambda _s: 99999)
    with pytest.raises(PermissionError):
        verify(object())
    # A recycled pid (pid 42 matches, but its live start-token differs) is refused.
    monkeypatch.setattr(mcp_secrets, "get_peer_pid", lambda _s: 42)
    monkeypatch.setattr(
        mcp_secrets.run_marker, "pid_start_token", lambda _pid: "recycled", raising=False
    )
    with pytest.raises(PermissionError):
        verify(object())
    # An absent run-marker record is refused (cannot authenticate the gateway).
    monkeypatch.setattr(
        mcp_secrets.run_marker, "read_pid_record_path", lambda _path: None, raising=False
    )
    with pytest.raises(PermissionError):
        verify(object())
    # An unreadable peer is refused too (fail closed).
    monkeypatch.setattr(mcp_secrets, "get_peer_pid", lambda _s: None)
    with pytest.raises(PermissionError):
        verify(object())


def test_http_error_surfaces_the_endpoints_safe_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_secrets, "_resolve_session_key", lambda: "sess")

    class _Err(urllib.error.HTTPError):
        def __init__(self) -> None:
            super().__init__("u", 403, "Forbidden", {}, None)  # type: ignore[arg-type]

        def read(self) -> bytes:  # type: ignore[override]
            return json.dumps({"error": "not authorized for this origin"}).encode()

    def _raise(_req: Any, _timeout: int) -> Any:
        raise _Err()

    _patch_transport(monkeypatch, urlopen=_raise)
    out = mcp_secrets._call_tool_inner(
        "call_api_with_secret",
        {"secret_name": "K", "method": "GET", "url": "https://api.example.com"},
    )
    assert out == "Error: not authorized for this origin"


def test_generic_failure_returns_a_safe_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_secrets, "_resolve_session_key", lambda: "sess")

    def _boom(_req: Any, _timeout: int) -> Any:
        raise RuntimeError("connection refused")

    _patch_transport(monkeypatch, urlopen=_boom)
    out = mcp_secrets._call_tool_inner(
        "call_api_with_secret",
        {"secret_name": "K", "method": "GET", "url": "https://api.example.com"},
    )
    assert out == "Error: The mediated request could not be completed."
    assert "connection refused" not in out


def test_preserve_leaves_an_unrelated_managed_servers_grant_alone() -> None:
    """Under 'preserve', a managed server keeps whatever autoApprove the user set,
    so a user who removed or kept a grant is not silently overridden."""
    from kiro_crew import agent

    entry = {"command": "kirocrew", "args": ["mcp-work"], "autoApprove": ["some_tool"]}
    spec = {"command": "kirocrew", "args": ["mcp-work"]}  # no grant in spec
    agent._enforce_managed_mcp_ownership(
        entry, spec, False, auto_approve="preserve", server_name="kirocrew-work"
    )
    assert entry.get("autoApprove") == ["some_tool"]


def test_an_auto_approved_call_mints_no_capability_so_dispatch_refuses() -> None:
    """An auto-approved (allowedTools) call never reaches ``approve_tool``,
    which is the ONLY site that mints a mediated-request capability. So no grant
    is minted for its tool_call_id, the capability claim finds no match, and the
    egress is refused at dispatch for lack of a capability — WITHOUT any
    install-time allowedTools scrub or mount-withhold. This is the invariant that
    makes those (removed) belt-and-suspenders redundant: the capability gate, not
    the mount shape, is what stops an auto-approved credential egress.
    """
    from kiro_crew import mediated_request_capability as cap

    # No approval ran for this call id, so nothing was minted (the auto-approved
    # path bypasses approve_tool). A claim therefore matches no grant.
    claimed = cap.claim("sess-key", "tool-call-never-approved", {"url": "https://api.example.com"})
    assert claimed == "", "an unapproved (auto-approved) call must not obtain a capability"
