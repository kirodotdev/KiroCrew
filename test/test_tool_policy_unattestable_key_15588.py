"""A key resolved from an unattestable source fails closed without a futile dial.

Reporter topology: a systemd system-service gateway on the kiro backend,
default agent, no custom agents. The gateway stamps no per-call caller block on the
tool-policy frame (the ``_ambient_audit_session`` fallback is active, attribution
only), and this process's MCP element carries no signed session token. The ONLY key
left is the lenient tail of ``_policy_session_key`` -- the unsigned
``session_pid_<pid>.txt`` read reached via ``KIROCREW_HOST_PID`` or the ancestor walk.

That key cannot be attested: ``session_token_header("")`` has no token to send, so a
policy request under it carries ``X-Session-Key`` with no ``X-Session-Token``. The
gateway answers ``409 member_identity_unavailable`` for that, so the dial is futile.
The resolver must not dial under a locally resolved key it has no attestation to
carry; it returns ``identity_unattestable`` -- a fail-CLOSED reason in
``_UNRESOLVED_REFUSES_CALL`` -- so ``tools/list`` stays complete while ``tools/call``
refuses (a key DID resolve, so an operator exclusion may exist and an unread exclusion
is an unknown deny, never a permission), recovering the instant a real identity
channel appears.
"""

from __future__ import annotations

import urllib.error
from unittest.mock import MagicMock

import pytest

from kiro_crew import mcp_shared
from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV

LENIENT_KEY = "dashboard:chat-9-current"


class _Body:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    def read(self) -> bytes:
        return self._raw

    def close(self) -> None:
        pass


def _refuse_unattested(req, timeout=None):
    raise urllib.error.HTTPError(
        url="",
        code=409,
        msg="unattested",
        hdrs=None,
        fp=_Body(b'{"code": "member_identity_unavailable"}'),
    )


@pytest.fixture
def dialled(monkeypatch, tmp_path):
    """A resolver primed to dial, recording every request it puts on the wire."""
    monkeypatch.delenv(STUB_SESSION_TOKEN_ENV, raising=False)
    monkeypatch.setattr(mcp_shared, "_last_failure_time", 0.0)
    monkeypatch.setattr(mcp_shared, "_last_startup_race_time", 0.0)
    monkeypatch.setattr(mcp_shared, "_last_startup_race_key", "")
    monkeypatch.setattr(mcp_shared, "_failure_count", 0)
    mcp_shared._excluded_tools_by_session.clear()
    monkeypatch.setattr(mcp_shared, "resolve_client_port_src", lambda _x: (5476, "test"))
    monkeypatch.setattr(mcp_shared, "read_local_secret", lambda _port: "test-secret")
    monkeypatch.setattr(mcp_shared, "current_caller", lambda: None)
    audit = MagicMock()
    monkeypatch.setattr(mcp_shared, "sel", lambda: audit)
    sent: list[dict] = []

    def record(req, timeout=None):
        sent.append(dict(req.headers))
        return _refuse_unattested(req, timeout)

    monkeypatch.setattr(mcp_shared, "loopback_urlopen", record)
    yield audit, sent
    mcp_shared._excluded_tools_by_session.clear()


def test_a_locally_resolved_key_with_no_token_is_not_sent(monkeypatch, dialled):
    """The reporter's steady state: a lenient key, no token, no gateway caller.

    The key resolves but has no attestation to carry, so the resolver must not dial
    the gateway under it -- that request can only ever be refused. It returns the
    fail-closed ``identity_unattestable`` reason without the key on the wire.
    """
    audit, sent = dialled
    monkeypatch.setattr(mcp_shared, "_policy_session_key", lambda: LENIENT_KEY)
    policy = mcp_shared._resolve_tool_policy()
    assert policy.unresolved == "identity_unattestable"
    # Fail CLOSED: a key resolved, so an operator exclusion may exist, and the
    # reason must make ``tools/call`` refuse rather than silently widen it.
    assert policy.unresolved in mcp_shared._UNRESOLVED_REFUSES_CALL
    assert sent == [], "an unattestable key was put on the wire"
    ops = [c.kwargs.get("operation") for c in audit.log_api_access.call_args_list]
    assert "tool_policy.unattestable_key" in ops


def test_a_locally_resolved_key_with_a_token_is_still_sent(monkeypatch, dialled):
    """A signed token on the element makes the key attestable, so it is sent."""
    audit, sent = dialled
    monkeypatch.setattr(mcp_shared, "_policy_session_key", lambda: LENIENT_KEY)
    monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, "f" * 64)
    monkeypatch.setattr(
        "kiro_crew.session_token_sig.session_token_header",
        lambda token="": {"X-Session-Token": "f" * 64},
    )

    def ok(req, timeout=None):
        sent.append(dict(req.headers))
        body = MagicMock()
        body.read.return_value = b'{"exclude": []}'
        body.__enter__ = MagicMock(return_value=body)
        body.__exit__ = MagicMock(return_value=False)
        return body

    monkeypatch.setattr(mcp_shared, "loopback_urlopen", ok)
    policy = mcp_shared._resolve_tool_policy()
    assert policy.unresolved == ""
    assert len(sent) == 1
    assert sent[0].get("X-session-key") == LENIENT_KEY
    assert sent[0].get("X-session-token") == "f" * 64


def test_a_gateway_stamped_caller_without_a_token_is_still_sent(monkeypatch, dialled):
    """A gateway-named caller is vouched for even with no per-call token, so it dials.

    ``caller_session`` is the gateway's own per-call identity; the gateway already
    named this caller, so the key is legitimate and the resolver must still ask under
    it. Only a LOCALLY resolved key with no token is withheld.
    """
    audit, sent = dialled
    policy = mcp_shared._resolve_tool_policy("dashboard:from-gateway")
    # It dialled (and got the 409 refusal), so the key reached the wire.
    assert len(sent) == 1
    assert sent[0].get("X-session-key") == "dashboard:from-gateway"
    assert policy.unresolved == "identity_unattested"
