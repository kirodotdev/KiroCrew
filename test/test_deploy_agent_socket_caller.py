"""An agent-socket call is an internal caller to every deploy guard.

The agent socket admits a verified capability with no ``X-Internal-Secret``
header, and the auth middleware marks that request ``internal_auth``. The deploy
guards must read the mark too, or a socket caller keeps ``confirm`` /
``override_scan`` and reaches every ``@_internal_denied`` handler.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.deploy import handlers, pending
from kiro_crew.mcp_tools import artifacts


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path: Path, _floor_monkeypatch):
    _floor_monkeypatch.setattr(
        pending, "_store_path", lambda: tmp_path / "deploy" / "pending-deploys.json"
    )


class _State:
    _restricted_keys: set = set()
    _slots: dict = {}


class _Req(dict):
    """A request double; a ``dict`` because ``web.Request`` is the mapping marks go in."""

    def __init__(self, body=None, *, headers=None, internal_auth=False):
        super().__init__()
        self._body = body or {}
        self.headers = {"X-Session-Key": "agent:s"} if headers is None else headers
        self.app = {"state": _State()}
        self.match_info: dict = {}
        if internal_auth:
            # What token_auth sets after it verifies a socket capability.
            self["internal_auth"] = True

    async def json(self):
        return self._body


def _socket(body=None) -> _Req:
    return _Req(body, internal_auth=True)


def _secret(body=None) -> _Req:
    # No mark: the header alone makes a caller internal.
    return _Req(body, headers={"X-Internal-Secret": "s", "X-Session-Key": "agent:s"})


@pytest.mark.parametrize("make", [_socket, _secret], ids=["socket", "secret-header"])
def test_internal_caller_has_confirm_and_override_scan_stripped(make):
    out = handlers._strip_confirm_for_internal(
        make(), {"site_id": "s", "confirm": True, "override_scan": True}
    )
    assert out == {"site_id": "s"}


def test_dashboard_caller_keeps_confirm():
    params = {"site_id": "s", "confirm": True, "override_scan": True}
    assert handlers._strip_confirm_for_internal(_Req(), dict(params)) == params


@pytest.mark.parametrize("make", [_socket, _secret], ids=["socket", "secret-header"])
def test_internal_caller_is_refused_by_an_internal_denied_handler(make):
    resp = asyncio.run(handlers._handle_recall(make({"site_id": "s"})))
    assert resp.status == 403
    assert "internal-secret callers" in json.loads(resp.body)["error"]


def test_a_mark_that_is_not_true_is_not_internal():
    req = _Req()
    req["internal_auth"] = "yes"
    assert handlers._is_internal_secret_request(req) is False


def test_socket_deploy_strips_confirm_before_the_engine(monkeypatch):
    seen: list[dict] = []

    async def _capture(params, **_k):
        seen.append(dict(params))
        return 200, {"requires_confirm": True}

    monkeypatch.setattr(handlers, "publish_denied_reason", lambda _r, _p, **_k: None)
    monkeypatch.setattr(handlers, "_do_deploy", _capture)
    body = {"site_id": "s", "artifact_slug": "a", "confirm": True, "override_scan": True}
    resp = asyncio.run(handlers._handle_deploy(_socket(body)))
    assert resp.status == 200
    assert seen == [{"site_id": "s", "artifact_slug": "a"}]


def test_deploy_artifact_tool_still_previews_over_the_socket(monkeypatch):
    """The shipped tool's only route is still open to a socket caller.

    Its request goes through the real ``_handle_deploy`` as a socket call; only
    the engine and the publish ceiling are stubbed.
    """
    seen: list[dict] = []

    async def _engine(params, **_k):
        seen.append(dict(params))
        return 200, {"requires_confirm": True, "public": True, "bytes": 1, "scan": "clean"}

    monkeypatch.setattr(handlers, "publish_denied_reason", lambda _r, _p, **_k: None)
    monkeypatch.setattr(handlers, "_do_deploy", _engine)

    def _post_over_socket(path, body):
        assert path == "/api/deploy/deploy"
        resp = asyncio.run(handlers._handle_deploy(_socket(body)))
        assert resp.status == 200
        return json.loads(resp.body)

    with patch("kiro_crew.mcp_core._post", side_effect=_post_over_socket):
        out = artifacts.deploy_artifact(
            "deploy_artifact", {"site_id": "s", "artifact_slug": "a", "ttl_hours": 72}
        )
    assert out.startswith("Deploy preview for site 's'")
    assert seen == [{"site_id": "s", "artifact_slug": "a", "ttl_hours": 72}]
    assert [e["site_id"] for e in pending.list_pending()] == ["s"]
