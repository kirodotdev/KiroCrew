"""An internal-route grant writes one SEL row, and its refusals keep theirs.

The loopback internal arm of ``token_auth_middleware`` admits a request that
carries the configured ``X-Internal-Secret``, or that the agent socket admitted
with a capability. Every MCP tool call and every session keepalive takes this
arm, so its grant row is the most frequent row in the security event log.

The grant is audited once, as ``internal_auth``/``granted`` with
``source=token_auth``. That row names the peer address (empty on a unix
socket) or the verified tailnet login, whether the request was forwarded, and
which credential admitted it. A second
``dashboard.token_auth`` row for the same grant, with the literal caller
``internal`` and no detail, would record nothing the first row lacks.

The secret refusals on this arm are separate decisions and each still writes
both of its rows, so a scan keyed on either operation still sees every one.
"""

from __future__ import annotations

import pytest
from token_auth_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _make_request,
    _ok_handler,
    clear_nonces,
)

import kiro_crew.dashboard.token_auth as token_auth_mod
from kiro_crew.dashboard.agent_ipc import GRANT_KEY, Binding
from kiro_crew.dashboard.token_auth import token_auth_middleware

_SECRET = "test-secret-123"
_PATH = "/api/session-keepalive"
_AUTH_OPERATIONS = frozenset({"internal_auth", "dashboard.token_auth"})


def _capture_sel(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    class _FakeSel:
        def log_api_access(self, **kw):
            calls.append(kw)

    monkeypatch.setattr(token_auth_mod, "_sel_fn", lambda: _FakeSel())
    return calls


def _auth_rows(calls: list[dict]) -> list[dict]:
    return [c for c in calls if c.get("operation") in _AUTH_OPERATIONS and c["resources"] == _PATH]


def _middleware(kind: str, secret: str = _SECRET):
    paths = frozenset({_PATH})
    if kind == "strict":
        return token_auth_middleware(internal_paths=paths, internal_secret=secret)
    return token_auth_middleware(mixed_internal_paths=paths, internal_secret=secret)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["strict", "mixed"])
async def test_internal_secret_grant_writes_one_row(monkeypatch, kind):
    calls = _capture_sel(monkeypatch)
    req = _make_request(_PATH, headers={"X-Internal-Secret": _SECRET}, method="POST")

    resp = await _middleware(kind)(req, _ok_handler)

    assert resp.status == 200
    rows = _auth_rows(calls)
    assert len(rows) == 1, rows
    (row,) = rows
    assert row["operation"] == "internal_auth"
    assert row["outcome"] == "granted"
    assert row["source"] == "token_auth"
    assert row["caller"] == "127.0.0.1"
    assert row["error"] == ""


@pytest.mark.asyncio
async def test_agent_socket_grant_writes_one_row_naming_its_credential(monkeypatch):
    calls = _capture_sel(monkeypatch)
    req = _make_request(_PATH, method="POST")
    state = {GRANT_KEY: Binding("kirocrew-core", "session-token", 4242)}
    req.get.side_effect = state.get
    req.__setitem__.side_effect = state.__setitem__

    resp = await _middleware("strict")(req, _ok_handler)

    assert resp.status == 200
    rows = _auth_rows(calls)
    assert len(rows) == 1, rows
    assert rows[0]["operation"] == "internal_auth"
    assert rows[0]["outcome"] == "granted"
    assert rows[0]["error"] == "agent socket capability"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configured", "provided", "detail"),
    [
        (_SECRET, "wrong", "wrong secret"),
        (_SECRET, "", "wrong secret"),
        ("", _SECRET, "no internal secret configured"),
    ],
)
async def test_internal_secret_refusal_keeps_both_rows(monkeypatch, configured, provided, detail):
    calls = _capture_sel(monkeypatch)
    req = _make_request(_PATH, headers={"X-Internal-Secret": provided}, method="POST")

    resp = await _middleware("strict", secret=configured)(req, _ok_handler)

    assert resp.status == 403
    rows = _auth_rows(calls)
    assert sorted(r["operation"] for r in rows) == ["dashboard.token_auth", "internal_auth"], rows
    assert {r["outcome"] for r in rows} == {"denied"}
    assert all(r["error"].startswith(detail) for r in rows), rows
