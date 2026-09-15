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

    monkeypatch.setattr(
        mcp_secrets.KiroCrewConfig,
        "load",
        classmethod(
            lambda cls: SimpleNamespace(dashboard=SimpleNamespace(url="http://127.0.0.1:8080"))
        ),
    )
    monkeypatch.setattr(mcp_secrets, "parse_dashboard_url", lambda _url: ("127.0.0.1", 8080))
    monkeypatch.setattr(mcp_secrets, "read_local_secret", lambda _port: secret)

    def _urlopen(req: Any, timeout: int = 0) -> Any:
        captured.append(req)
        return urlopen(req, timeout)

    monkeypatch.setattr(mcp_secrets, "loopback_urlopen", _urlopen)
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


def test_resolve_session_key_prefers_the_protected_member_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_secrets, "protected_member_session_for_pid", lambda _pid: "member-sess")
    assert mcp_secrets._resolve_session_key() == "member-sess"


def test_resolve_session_key_falls_back_to_the_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_secrets, "protected_member_session_for_pid", lambda _pid: "")
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "env-sess")
    assert mcp_secrets._resolve_session_key() == "env-sess"


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


def test_forwards_the_member_proof_header_when_a_caller_is_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.member_memory_auth import PROOF_HEADER

    monkeypatch.setattr(mcp_secrets, "_resolve_session_key", lambda: "sess")

    def _ok(_req: Any, _timeout: int) -> _FakeResp:
        return _FakeResp({"status": 200, "headers": {}, "body": "", "truncated": False})

    captured = _patch_transport(monkeypatch, urlopen=_ok)

    # A caller carrying a valid-shaped proof; current_caller is imported at
    # module scope in mcp_secrets, so patch the name bound there.
    monkeypatch.setattr(
        mcp_secrets,
        "current_caller",
        lambda: SimpleNamespace(member_memory_proof="cGF5bG9hZA.deadbeef"),
    )

    mcp_secrets._call_tool_inner(
        "call_api_with_secret",
        {"secret_name": "K", "method": "GET", "url": "https://api.example.com"},
    )
    assert captured, "the forwarder should have issued a loopback request"
    assert captured[0].headers.get(PROOF_HEADER.capitalize()) == "cGF5bG9hZA.deadbeef"


def test_self_mints_the_proof_for_a_direct_member_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the gateway did NOT inject a caller-meta proof (a direct member MCP
    call), the forwarder mints its OWN audience-bound proof from its protected
    member pid+session so the documented member-chat flow is reachable."""
    from kiro_crew.member_memory_auth import PROOF_HEADER

    monkeypatch.setattr(mcp_secrets, "_resolve_session_key", lambda: "member:alice")
    # No gateway-injected proof.
    monkeypatch.setattr(mcp_secrets, "current_caller", lambda: None)
    # The self-mint primitive returns a valid-shaped proof for this member pid.
    import kiro_crew.member_memory_auth as auth_mod

    monkeypatch.setattr(
        auth_mod, "issue_member_session_proof", lambda sk, pid, *, audience: "c2VsZg.beef"
    )

    def _ok(_req: Any, _timeout: int) -> _FakeResp:
        return _FakeResp({"status": 200, "headers": {}, "body": "", "truncated": False})

    captured = _patch_transport(monkeypatch, urlopen=_ok)
    mcp_secrets._call_tool_inner(
        "call_api_with_secret",
        {"secret_name": "K", "method": "GET", "url": "https://api.example.com"},
    )
    assert captured, "the forwarder should have issued a loopback request"
    assert captured[0].headers.get(PROOF_HEADER.capitalize()) == "c2VsZg.beef"


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


def test_inherited_auto_approve_is_stripped_from_a_no_grant_managed_server() -> None:
    """A managed server whose spec ships NO autoApprove grant must never carry an
    inherited one — even under 'preserve' mode on an existing config. Otherwise a
    prior/wildcard autoApprove on kirocrew-secrets would transfer and silently
    bypass the approval gate on a credential-bearing egress."""
    from kiro_crew import agent

    entry = {
        "command": "kirocrew",
        "args": ["mcp-secrets"],
        "autoApprove": ["call_api_with_secret"],
    }
    spec = {"command": "kirocrew", "args": ["mcp-secrets"]}  # no autoApprove grant
    agent._enforce_managed_mcp_ownership(
        entry, spec, False, auto_approve="preserve", server_name="kirocrew-secrets"
    )
    assert "autoApprove" not in entry


def test_preserve_leaves_an_unrelated_managed_servers_grant_alone() -> None:
    """The never-auto-approve strip is scoped to credential servers: an unrelated
    managed server under 'preserve' keeps whatever autoApprove the user set (so a
    user who removed or kept a grant is not silently overridden)."""
    from kiro_crew import agent

    entry = {"command": "kirocrew", "args": ["mcp-work"], "autoApprove": ["some_tool"]}
    spec = {"command": "kirocrew", "args": ["mcp-work"]}  # no grant in spec
    agent._enforce_managed_mcp_ownership(
        entry, spec, False, auto_approve="preserve", server_name="kirocrew-work"
    )
    assert entry.get("autoApprove") == ["some_tool"]


def test_grant_matcher_scrubs_exact_serverwide_and_partial_globs() -> None:
    """Any allowedTools grant that would auto-approve call_api_with_secret must be
    detected — exact ref, server-wide, a bare '*', AND partial globs / the bare
    tool name — so none slips an auto-approve past the credential gate."""
    from kiro_crew.agent import _grant_reaches_secret_tool as m

    # Grants that DO reach the mediated tool → must be scrubbed.
    for g in [
        "*",
        "@kirocrew-secrets",
        "kirocrew-secrets",
        "@kirocrew-secrets/*",
        "@kirocrew-secrets/call_api_with_secret",
        "@kirocrew-secrets/call_*",
        "@kirocrew*",
        "call_api_with_secret",
        "call_*",
        "@kirocrew-secrets/call_api_with_secre?",
    ]:
        assert m(g), f"expected {g!r} to be treated as granting the mediated tool"

    # Grants that do NOT reach it → must be preserved.
    for g in [
        "@kirocrew-core",
        "@kirocrew-core/*",
        "fs_read",
        "@kirocrew-cron/schedule",
        "",
        "   ",
        123,  # non-string
        "@kirocrew-secretsX",  # different server, not a glob
    ]:
        assert not m(g), f"expected {g!r} NOT to be treated as granting the mediated tool"
