"""Route-level tests for the Plane C wake handlers.

Exercises owner resolution (from the authenticated header, never a body field),
the poll/ack contract, and the inspect-by-handle read. Admission is covered
separately in ``test_supervised_phase4_admissions.py``; these tests assume the
request already passed the middleware and carries ``internal_auth``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import make_mocked_request

import kiro_crew.dashboard.handlers.crew_wakes as cw
from kiro_crew.crew_wakes import WakeQueue

pytestmark = pytest.mark.asyncio

OWNER = "kiro-cli:sess-1"


def _state(wq: WakeQueue) -> SimpleNamespace:
    return SimpleNamespace(wake_queue=lambda: wq)


def _poll_req(path: str, *, owner: str | None = OWNER, internal: bool = True, wq: WakeQueue = None):
    """A GET request for the poll handler (no body)."""
    headers = {}
    if owner is not None:
        headers["X-Session-Key"] = owner
    req = make_mocked_request("GET", path, headers=headers)
    req["internal_auth"] = internal
    req.app["state"] = _state(wq)
    return req


def _ack_req(wake_id: str, *, owner: str | None = OWNER, internal: bool = True, body=None,
             wq: WakeQueue = None):
    """A POST ack request. Uses a MagicMock so ``request.json()`` returns *body*."""
    from unittest.mock import AsyncMock, MagicMock

    req = MagicMock()
    req.app = {"state": _state(wq)}
    req.get = lambda k, d=None: (internal if k == "internal_auth" else d)
    req.headers = {"X-Session-Key": owner} if owner is not None else {}
    req.match_info = {"wake_id": wake_id}
    req.json = AsyncMock(return_value=body)
    return req


# -- owner gate ----------------------------------------------------------------


async def test_poll_requires_internal_auth(tmp_path) -> None:
    wq = WakeQueue(tmp_path)
    resp = await cw.api_crew_wakes_poll(_poll_req("/api/crew/wakes", internal=False, wq=wq))
    assert resp.status == 403
    assert json.loads(resp.body)["code"] == "internal_required"


async def test_poll_requires_session_key(tmp_path) -> None:
    wq = WakeQueue(tmp_path)
    resp = await cw.api_crew_wakes_poll(_poll_req("/api/crew/wakes", owner=None, wq=wq))
    assert resp.status == 400
    assert json.loads(resp.body)["code"] == "session_required"


# -- poll ----------------------------------------------------------------------


async def test_poll_returns_owner_wakes(tmp_path) -> None:
    wq = WakeQueue(tmp_path)
    await wq.enqueue(OWNER, "monitor", "loop1", "do the thing", cycle=2, reason="changed")
    await wq.enqueue("kiro-cli:other", "cron", "job9", "not mine")
    resp = await cw.api_crew_wakes_poll(_poll_req("/api/crew/wakes?wait=0", wq=wq))
    assert resp.status == 200
    body = json.loads(resp.body)
    assert len(body["wakes"]) == 1
    w = body["wakes"][0]
    assert w["kind"] == "monitor" and w["handle"] == "loop1"
    assert w["message"] == "do the thing" and w["cycle"] == 2 and w["reason"] == "changed"
    assert "id" in w


async def test_poll_rejects_non_numeric_wait(tmp_path) -> None:
    wq = WakeQueue(tmp_path)
    resp = await cw.api_crew_wakes_poll(_poll_req("/api/crew/wakes?wait=soon", wq=wq))
    assert resp.status == 400
    assert json.loads(resp.body)["code"] == "invalid_wait"


# -- ack -----------------------------------------------------------------------


async def test_ack_submitted_removes_and_reports_true(tmp_path) -> None:
    wq = WakeQueue(tmp_path)
    w = await wq.enqueue(OWNER, "monitor", "loop1", "m")
    resp = await cw.api_crew_wakes_ack(_ack_req(w.id, body={"outcome": "submitted"}, wq=wq))
    assert resp.status == 200
    assert json.loads(resp.body)["acked"] is True
    assert await wq.peek(OWNER) == []


async def test_ack_unknown_id_reports_false(tmp_path) -> None:
    wq = WakeQueue(tmp_path)
    resp = await cw.api_crew_wakes_ack(_ack_req("nope", body={"outcome": "dropped"}, wq=wq))
    assert resp.status == 200
    assert json.loads(resp.body)["acked"] is False


async def test_ack_rejects_bad_outcome(tmp_path) -> None:
    wq = WakeQueue(tmp_path)
    w = await wq.enqueue(OWNER, "monitor", "loop1", "m")
    resp = await cw.api_crew_wakes_ack(_ack_req(w.id, body={"outcome": "maybe"}, wq=wq))
    assert resp.status == 400
    assert json.loads(resp.body)["code"] == "invalid_outcome"
    # Not removed on a rejected ack.
    assert len(await wq.peek(OWNER)) == 1


async def test_ack_cannot_reach_another_owners_wake(tmp_path) -> None:
    wq = WakeQueue(tmp_path)
    theirs = await wq.enqueue("kiro-cli:other", "monitor", "loop1", "m")
    resp = await cw.api_crew_wakes_ack(_ack_req(theirs.id, body={"outcome": "submitted"}, wq=wq))
    # Acking as OWNER does not find another owner's wake.
    assert json.loads(resp.body)["acked"] is False
    assert len(await wq.peek("kiro-cli:other")) == 1


# -- inspect-by-handle ---------------------------------------------------------


async def test_autonudge_get_by_id_returns_loop(monkeypatch) -> None:
    from kiro_crew.autonudge import NudgeLoop

    loop = NudgeLoop(id="lp-9", slot_key="kiro-cli:sess-1", message="m", idle_secs=300)

    class _Svc:
        def get_by_id(self, lid):
            return loop if lid == "lp-9" else None

    monkeypatch.setattr(cw, "_autonudge_get", lambda: _Svc())
    req = make_mocked_request("GET", "/api/autonudge/lp-9", match_info={"loop_id": "lp-9"})
    resp = await cw.api_autonudge_get_by_id(req)
    body = json.loads(resp.body)
    assert body["enabled"] is True
    assert body["loop"]["id"] == "lp-9"
    assert body["isMonitor"] is False


async def test_autonudge_get_by_id_null_for_unknown(monkeypatch) -> None:
    class _Svc:
        def get_by_id(self, lid):
            return None

    monkeypatch.setattr(cw, "_autonudge_get", lambda: _Svc())
    req = make_mocked_request("GET", "/api/autonudge/nope", match_info={"loop_id": "nope"})
    resp = await cw.api_autonudge_get_by_id(req)
    body = json.loads(resp.body)
    assert body["loop"] is None
