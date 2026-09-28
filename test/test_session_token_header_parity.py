"""Every internal request that DECLARES a session key must also prove it.

Two edits make one working caller: sending ``X-Session-Key`` and sending the
signed ``X-Session-Token`` that vouches for it. Nothing couples them, and the
gateway's session-scoped routes answer a declared key without proof as a caller
they cannot name -- which for the managed tool policy means the policy is
unresolved and the fail-closed branch refuses every tool call for that session.
So a helper that omits the header does not fail where it is written; it fails as
a whole tool surface going dark.

This file is the coupling. It drives each transport helper for real and asserts
on the headers that leave, so a new verb, or a hand-built request that sets the
key itself, has to carry the token to pass.
"""

from __future__ import annotations

import io

import pytest

from kiro_crew import mcp_core, mcp_shared
from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV

_TOKEN = "d" * 64
_SESSION = "dashboard:chat-7"


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


@pytest.fixture
def sent(monkeypatch):
    """Capture the headers each ``mcp_core`` helper hands the transport."""
    captured: list[dict[str, str]] = []

    def fake_send(path, *, data=None, headers=None, method="GET", timeout=0, **kwargs):
        captured.append(dict(headers or {}))
        return {}

    monkeypatch.setattr(mcp_core, "_send", fake_send)
    monkeypatch.setattr(mcp_core, "_internal_secret", lambda: "s3cr3t")
    monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: _SESSION)
    return captured


def _verbs() -> list[tuple[str, object]]:
    """Every transport helper that can attach ``X-Session-Key``.

    Listed by NAME so a helper added to ``mcp_core`` without an entry here is a
    visible omission in one place rather than an untested call site.
    """
    return [
        ("_get", lambda: mcp_core._get("/api/thing")),
        ("_post", lambda: mcp_core._post("/api/thing", {})),
        ("_patch", lambda: mcp_core._patch("/api/thing", {})),
        ("_put", lambda: mcp_core._put("/api/thing", {})),
        ("_delete", lambda: mcp_core._delete("/api/thing")),
    ]


@pytest.mark.parametrize("name,call", _verbs(), ids=[name for name, _ in _verbs()])
def test_every_verb_that_declares_a_key_also_sends_the_token(sent, monkeypatch, name, call):
    monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, _TOKEN)

    call()

    assert sent, f"{name} reached no transport"
    headers = sent[-1]
    assert headers.get("X-Session-Key") == _SESSION, name
    assert headers.get("X-Session-Token") == _TOKEN, (
        f"{name} declares a session key with no attestation behind it; the "
        "gateway answers it as a caller it cannot name"
    )


@pytest.mark.parametrize("name,call", _verbs(), ids=[name for name, _ in _verbs()])
def test_a_process_with_no_token_sends_the_request_it_always_sent(sent, monkeypatch, name, call):
    """Absence stays absent: an empty header would name a token nothing verifies."""
    monkeypatch.delenv(STUB_SESSION_TOKEN_ENV, raising=False)

    call()

    assert "X-Session-Token" not in sent[-1], name


def test_the_tool_policy_read_proves_the_key_it_declares(monkeypatch):
    """The one hand-built request, and the one whose failure silences every tool."""
    seen: list[dict[str, str]] = []

    def fake_urlopen(req, timeout=0):
        seen.append({k: v for k, v in req.headers.items()})
        return _Resp(b'{"exclude": []}')

    monkeypatch.setenv(STUB_SESSION_TOKEN_ENV, _TOKEN)
    monkeypatch.setattr(mcp_shared, "loopback_urlopen", fake_urlopen)
    monkeypatch.setattr(mcp_shared, "resolve_client_port_src", lambda port: (5476, "config"))
    monkeypatch.setattr(mcp_shared, "_read_internal_secret", lambda: "s3cr3t", raising=False)

    policy = mcp_shared._resolve_tool_policy(_SESSION)

    assert policy.unresolved == ""
    assert seen, "the policy read reached no transport"
    # urllib title-cases header names on the request object.
    headers = {key.lower(): value for key, value in seen[-1].items()}
    assert headers.get("x-session-key") == _SESSION
    assert headers.get("x-session-token") == _TOKEN
