"""Coverage tests for the auto-nudge HTTP mapping (``dashboard/handlers/autonudge.py``).

``test_autonudge.py`` drives ``AutoNudgeService`` itself and ``test_autonudge_stop_auth.py``
drives the transport-agnostic authorizer. What neither touches is the thin HTTP layer
between them: the read routes (list / get), the "service is absent" 503+``enabled: false``
shapes, the malformed-body 400s, and the DELETE route's audit record — which has to name
the removed loop's ``slot_key`` even though the loop is gone by the time it logs.

Everything is driven through aiohttp's ``make_mocked_request`` (no socket bound) against a
fake service, so no timer task is armed and no loop store is written. ``sel()`` is replaced
with a mock so the audit call can be asserted on rather than appended to a real event log.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.autonudge import NudgeLoop
from kiro_crew.dashboard.handlers import autonudge as h


class _FakeSvc:
    """Just the four methods the HTTP layer calls on the service."""

    def __init__(self, loops: list[NudgeLoop] | None = None) -> None:
        self.loops = loops or []
        self.removed: list[str] = []

    def list_all(self) -> list[NudgeLoop]:
        return list(self.loops)

    def get_by_slot(self, slot_key: str) -> NudgeLoop | None:
        return next((lp for lp in self.loops if lp.slot_key == slot_key), None)

    async def remove(self, loop_id: str) -> None:
        self.removed.append(loop_id)


def _loop(loop_id: str = "lp-1", slot_key: str = "chat-1-111") -> NudgeLoop:
    return NudgeLoop(id=loop_id, slot_key=slot_key, message="keep checking", idle_secs=300)


@pytest.fixture()
def sel_mock(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace the audit sink so nothing is written to a real event log."""
    sink = MagicMock()
    monkeypatch.setattr(h, "sel", lambda: sink)
    return sink


def _svc(monkeypatch: pytest.MonkeyPatch, svc: Any) -> Any:
    monkeypatch.setattr(h, "_autonudge_get", lambda: svc)
    return svc


def _mk(
    method: str,
    path: str,
    *,
    match: dict[str, str] | None = None,
    body: Any = ...,
    state: Any = None,
) -> web.Request:
    app = web.Application()
    app["state"] = state if state is not None else MagicMock()
    req = make_mocked_request(method, path, app=app, match_info=match or {})
    if body is not ...:
        if body is None:
            req.json = AsyncMock(side_effect=ValueError("bad json"))  # type: ignore[method-assign]
        else:
            req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


def _body(response: web.StreamResponse) -> dict:
    assert isinstance(response, web.Response)
    raw = response.body
    assert isinstance(raw, bytes)
    return json.loads(raw.decode("utf-8"))


# --- GET /api/autonudge ------------------------------------------------------


@pytest.mark.asyncio
async def test_list_reports_disabled_when_service_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    _svc(monkeypatch, None)
    response = await h.api_autonudge_list(_mk("GET", "/api/autonudge"))
    assert _body(response) == {"enabled": False, "loops": []}


@pytest.mark.asyncio
async def test_list_serializes_every_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    _svc(monkeypatch, _FakeSvc([_loop("lp-1"), _loop("lp-2", "chat-2-222")]))
    payload = _body(await h.api_autonudge_list(_mk("GET", "/api/autonudge")))
    assert payload["enabled"] is True
    assert [lp["id"] for lp in payload["loops"]] == ["lp-1", "lp-2"]
    # asdict() round-trip, not a repr: the full dataclass shape reaches the client.
    assert payload["loops"][0]["idle_secs"] == 300
    assert payload["loops"][0]["slot_key"] == "chat-1-111"


# --- GET /api/autonudge/{slot_key} -------------------------------------------


@pytest.mark.asyncio
async def test_get_reports_disabled_when_service_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    _svc(monkeypatch, None)
    request = _mk("GET", "/api/autonudge/chat-1-111", match={"slot_key": "chat-1-111"})
    assert _body(await h.api_autonudge_get(request)) == {"enabled": False, "loop": None}


@pytest.mark.asyncio
async def test_get_returns_the_loop_bound_to_the_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    _svc(monkeypatch, _FakeSvc([_loop("lp-9", "chat-7-777")]))
    request = _mk("GET", "/api/autonudge/chat-7-777", match={"slot_key": "chat-7-777"})
    payload = _body(await h.api_autonudge_get(request))
    assert payload["enabled"] is True
    assert payload["loop"]["id"] == "lp-9"


@pytest.mark.asyncio
async def test_get_returns_null_loop_for_an_unbound_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    _svc(monkeypatch, _FakeSvc([_loop("lp-9", "chat-7-777")]))
    request = _mk("GET", "/api/autonudge/chat-8-888", match={"slot_key": "chat-8-888"})
    assert _body(await h.api_autonudge_get(request)) == {"enabled": True, "loop": None}


# --- POST /api/autonudge -----------------------------------------------------


@pytest.mark.asyncio
async def test_start_503_when_service_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    _svc(monkeypatch, None)
    response = await h.api_autonudge_start(_mk("POST", "/api/autonudge", body={}))
    assert response.status == 503
    assert _body(response)["code"] == "autonudge_disabled"


@pytest.mark.asyncio
async def test_start_400_on_undecodable_body(monkeypatch: pytest.MonkeyPatch) -> None:
    _svc(monkeypatch, _FakeSvc())
    response = await h.api_autonudge_start(_mk("POST", "/api/autonudge", body=None))
    assert response.status == 400
    assert _body(response) == {"error": "invalid JSON"}


@pytest.mark.asyncio
async def test_start_rejects_a_fractional_idle_secs(monkeypatch: pytest.MonkeyPatch) -> None:
    _svc(monkeypatch, _FakeSvc())
    request = _mk("POST", "/api/autonudge", body={"slot_key": "s", "idle_secs": 1.5})
    response = await h.api_autonudge_start(request)
    assert response.status == 400
    assert _body(response)["code"] == "not_a_whole_number"


@pytest.mark.asyncio
async def test_start_rejects_a_non_integer_max_cycles(monkeypatch: pytest.MonkeyPatch) -> None:
    _svc(monkeypatch, _FakeSvc())
    request = _mk("POST", "/api/autonudge", body={"slot_key": "s", "max_cycles": "abc"})
    response = await h.api_autonudge_start(request)
    assert response.status == 400
    assert "integers" in _body(response)["error"]


@pytest.mark.asyncio
async def test_start_passes_coerced_values_to_the_authorizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """session_key wins over slot_key, and the three numbers arrive as ints."""
    svc = _svc(monkeypatch, _FakeSvc())
    authorize = AsyncMock(return_value=(_loop("lp-new"), None, 200))
    monkeypatch.setattr(h, "authorize_and_add_nudge", authorize)
    request = _mk(
        "POST",
        "/api/autonudge",
        body={
            "session_key": "chat-3-333",
            "slot_key": "ignored",
            "message": "poll it",
            "idle_secs": 120.0,
            "max_cycles": 4,
            "max_runtime_secs": 900,
        },
    )
    payload = _body(await h.api_autonudge_start(request))
    assert payload["ok"] is True
    assert payload["loop"]["id"] == "lp-new"
    assert authorize.await_args is not None
    kwargs = authorize.await_args.kwargs
    assert kwargs["svc"] is svc
    assert kwargs["slot_key"] == "chat-3-333"
    assert (kwargs["idle_secs"], kwargs["max_cycles"], kwargs["max_runtime_secs"]) == (120, 4, 900)
    assert kwargs["source"] == "dashboard"


@pytest.mark.asyncio
async def test_start_surfaces_the_authorizer_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    _svc(monkeypatch, _FakeSvc())
    monkeypatch.setattr(
        h, "authorize_and_add_nudge", AsyncMock(return_value=(None, "slot not yours", 403))
    )
    request = _mk("POST", "/api/autonudge", body={"slot_key": "chat-1-111", "message": "go"})
    response = await h.api_autonudge_start(request)
    assert response.status == 403
    assert _body(response) == {"error": "slot not yours"}


# --- PATCH /api/autonudge/{loop_id} ------------------------------------------


@pytest.mark.asyncio
async def test_update_503_when_service_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    _svc(monkeypatch, None)
    request = _mk("PATCH", "/api/autonudge/lp-1", match={"loop_id": "lp-1"}, body={})
    response = await h.api_autonudge_update(request)
    assert response.status == 503
    assert _body(response)["code"] == "autonudge_disabled"


@pytest.mark.asyncio
async def test_update_400_on_undecodable_body(monkeypatch: pytest.MonkeyPatch) -> None:
    _svc(monkeypatch, _FakeSvc())
    request = _mk("PATCH", "/api/autonudge/lp-1", match={"loop_id": "lp-1"}, body=None)
    response = await h.api_autonudge_update(request)
    assert response.status == 400
    assert _body(response) == {"error": "invalid JSON"}


@pytest.mark.asyncio
async def test_update_forwards_raw_fields_to_the_authorizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The HTTP layer coerces nothing here — the authorizer owns that."""
    _svc(monkeypatch, _FakeSvc())
    authorize = AsyncMock(return_value=(_loop("lp-1"), None, 200))
    monkeypatch.setattr(h, "authorize_and_update_nudge", authorize)
    request = _mk(
        "PATCH",
        "/api/autonudge/lp-1",
        match={"loop_id": "lp-1"},
        body={"message": "new", "idle_secs": "900", "active": False},
    )
    payload = _body(await h.api_autonudge_update(request))
    assert payload == {"ok": True, "loop": h._serialize(_loop("lp-1"))}
    assert authorize.await_args is not None
    kwargs = authorize.await_args.kwargs
    assert kwargs["loop_id"] == "lp-1"
    assert kwargs["idle_secs"] == "900"
    assert kwargs["active"] is False
    assert kwargs["max_cycles"] is None and kwargs["max_runtime_secs"] is None


@pytest.mark.asyncio
async def test_update_surfaces_the_authorizer_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    _svc(monkeypatch, _FakeSvc())
    monkeypatch.setattr(
        h, "authorize_and_update_nudge", AsyncMock(return_value=(None, "no such loop", 404))
    )
    request = _mk("PATCH", "/api/autonudge/lp-x", match={"loop_id": "lp-x"}, body={"active": True})
    response = await h.api_autonudge_update(request)
    assert response.status == 404
    assert _body(response) == {"error": "no such loop"}


# --- DELETE /api/autonudge/{loop_id} -----------------------------------------


@pytest.mark.asyncio
async def test_delete_503_when_service_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    _svc(monkeypatch, None)
    request = _mk("DELETE", "/api/autonudge/lp-1", match={"loop_id": "lp-1"})
    response = await h.api_autonudge_delete(request)
    assert response.status == 503
    assert _body(response)["code"] == "autonudge_disabled"


@pytest.mark.asyncio
async def test_delete_removes_and_audits_the_owning_slot(
    monkeypatch: pytest.MonkeyPatch, sel_mock: MagicMock
) -> None:
    """slot_key must be captured BEFORE remove(), or the audit record is anonymous."""
    svc = _svc(monkeypatch, _FakeSvc([_loop("lp-1", "chat-5-555")]))
    request = _mk("DELETE", "/api/autonudge/lp-1", match={"loop_id": "lp-1"})
    assert _body(await h.api_autonudge_delete(request)) == {"ok": True}
    assert svc.removed == ["lp-1"]
    kwargs = sel_mock.log_tool_invocation.call_args.kwargs
    assert kwargs["session_key"] == "chat-5-555"
    assert kwargs["tool_name"] == "autonudge_delete"
    assert kwargs["outcome"] == "success"
    assert kwargs["metadata"]["loop_id"] == "lp-1"


@pytest.mark.asyncio
async def test_delete_of_an_unknown_loop_is_audited_as_a_noop(
    monkeypatch: pytest.MonkeyPatch, sel_mock: MagicMock
) -> None:
    svc = _svc(monkeypatch, _FakeSvc([_loop("lp-1", "chat-5-555")]))
    request = _mk("DELETE", "/api/autonudge/lp-gone", match={"loop_id": "lp-gone"})
    assert _body(await h.api_autonudge_delete(request)) == {"ok": True}
    assert svc.removed == ["lp-gone"]
    kwargs = sel_mock.log_tool_invocation.call_args.kwargs
    assert kwargs["outcome"] == "noop"
    assert kwargs["session_key"] == ""
