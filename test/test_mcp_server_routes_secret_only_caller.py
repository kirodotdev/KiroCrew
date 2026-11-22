"""``PUT`` and ``DELETE /api/mcp/servers/{name}`` refuse a caller that holds only the secret.

The route writes the ``command`` that later agent sessions run. A request the token
middleware marked ``internal_auth`` proves a local process held ``X-Internal-Secret``
and nothing more, so the route also asks for an attested session. The dashboard owner
is judged by the owner gate as before. These cases call the handler with a constructed
request; no server starts.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from handlers_mcp_coverage_helpers import sandbox_fixture  # noqa: F401
from handlers_mcp_coverage_helpers import _payload, _request

from kiro_crew.dashboard.handlers import mcp as mcp_mod

_NAME = "newsrv"
_BODY = {"command": "node", "args": ["server.js"]}


@pytest.fixture(autouse=True)
def _owned_home(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """The agent home is ours, so a passing caller reaches the mcp.json write."""
    _floor_monkeypatch.setattr(mcp_mod, "_agent_home_not_owned", lambda _name: None)


@pytest.fixture(name="writes")
def writes_fixture(sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Every config the handler tried to write; the server to delete is already there."""
    sandbox.global_json.write_text(
        json.dumps({"mcpServers": {_NAME: {"command": "old"}}}), encoding="utf-8"
    )
    seen: list[dict] = []
    original = mcp_mod._write_mcp_json

    def _record(data: dict) -> None:
        seen.append(json.loads(json.dumps(data)))
        original(data)

    monkeypatch.setattr(mcp_mod, "_write_mcp_json", _record)
    return seen


def _call(method: str, claims: dict, headers: dict | None = None):
    request = _request(
        _BODY if method == "PUT" else None, match_info={"name": _NAME}, method=method
    )
    state = dict(claims)
    request.get = lambda key, default=None: state.get(key, default)
    for hook, impl in (
        (request.__contains__, lambda key: key in state),
        (request.__getitem__, lambda key: state[key]),
    ):
        hook.side_effect = impl
    request.headers = dict(headers or {})
    return mcp_mod.api_mcp_server_detail(request)


_SECRET_ONLY = {"internal_auth": True}
_BACKEND_WITH_SESSION_NAME = ({"internal_auth": True}, {"X-Session-Key": "backend-session"})
_ATTESTED = (
    {"internal_auth": True, "peer_verified": True},
    {"X-Session-Key": "agent-session"},
)
_OWNER = {"user": "local-app", "app": ""}
_APP_CLAIM = {"user": "local-app", "app": "some-app"}


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ("PUT", "DELETE"))
class TestARefusedCaller:
    async def test_the_secret_alone_is_refused(self, method, writes):
        resp = await _call(method, _SECRET_ONLY)

        assert resp.status == 403, _payload(resp)
        assert writes == []

    async def test_the_secret_with_a_session_name_nobody_attests_is_refused(self, method, writes):
        """A process that read the secret can name any session key; the name proves nothing."""
        claims, headers = _BACKEND_WITH_SESSION_NAME

        resp = await _call(method, claims, headers)

        assert resp.status == 403, _payload(resp)
        assert writes == []

    async def test_a_caller_with_an_app_claim_is_refused(self, method, writes):
        """GUARD: stays refused."""
        resp = await _call(method, _APP_CLAIM)

        assert resp.status == 403, _payload(resp)
        assert writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ("PUT", "DELETE"))
class TestAnAdmittedCaller:
    async def test_the_owner_still_writes(self, method, writes):
        """GUARD: the owner gate is unchanged."""
        resp = await _call(method, _OWNER)

        assert resp.status == 200, _payload(resp)
        assert len(writes) == 1

    async def test_an_attested_session_with_the_secret_still_writes(self, method, writes):
        """GUARD: the gateway's own placed session, attested by its transport."""
        claims, headers = _ATTESTED

        resp = await _call(method, claims, headers)

        assert resp.status == 200, _payload(resp)
        assert len(writes) == 1


@pytest.fixture(name="audit")
def audit_fixture(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Every ``log_api_access`` event the route writes; a bare name is answered by a no-op."""
    events: list[dict] = []

    class _Audit:
        def log_api_access(self, **event):
            events.append(event)

        def __getattr__(self, _name):
            return lambda *args, **kwargs: None

    monkeypatch.setattr("kiro_crew.sel.sel", lambda: _Audit())
    return events


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ("PUT", "DELETE"))
class TestARefusalIsAudited:
    async def test_the_secret_alone_leaves_a_denied_row(self, method, writes, audit):
        resp = await _call(method, _SECRET_ONLY)

        assert resp.status == 403, _payload(resp)
        assert [(e["operation"], e["outcome"]) for e in audit] == [("mcp_server_detail", "denied")]

    async def test_an_unattested_session_name_leaves_a_denied_row(self, method, writes, audit):
        claims, headers = _BACKEND_WITH_SESSION_NAME

        resp = await _call(method, claims, headers)

        assert resp.status == 403, _payload(resp)
        assert [(e["operation"], e["outcome"]) for e in audit] == [("mcp_server_detail", "denied")]

    async def test_a_failing_audit_still_refuses(self, method, writes, monkeypatch):
        """The audit never changes the answer."""

        def _broken():
            raise RuntimeError("audit down")

        monkeypatch.setattr("kiro_crew.sel.sel", _broken)

        resp = await _call(method, _SECRET_ONLY)

        assert resp.status == 403, _payload(resp)
        assert writes == []

    async def test_an_attested_caller_leaves_no_denied_row(self, method, writes, audit):
        """GUARD: an admitted caller is not audited as denied."""
        claims, headers = _ATTESTED

        resp = await _call(method, claims, headers)

        assert resp.status == 200, _payload(resp)
        assert [e for e in audit if e.get("outcome") == "denied"] == []
