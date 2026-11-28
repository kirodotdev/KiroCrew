"""A ``session_send`` or ``session_create`` whose reply is lost is not done twice.

A slow gateway can outlast the MCP client's wait. The client then reports a lost
reply (``transport_error``) while the route still delivers the message or opens the
session, so a blind retry would send the message twice or leave an empty session
behind. The tools post one ``request_id`` per call and resend it while the reply is
lost; the routes answer a resend from the first reply
(``dashboard.session_control_replay``). Pinned here:

* the tool resends a lost send or create under the same id and reports the reply;
* a refusal is never resent, and replies lost on every try say the work may have
  happened and must not be repeated;
* a resend to a gateway that does not echo the id is reported as a possible double;
* the send and create routes deliver and create once for a repeated id, both in
  sequence and while the first request is still running;
* a reused id with a different body, a malformed id, and another caller's id.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from chat_test_helpers import _make_state
from session_control_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _key,
    _peer_target,
    _slot,
)

from kiro_crew import mcp_session_once
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard import session_control_replay
from kiro_crew.dashboard.handlers import session_control as handlers_sc
from kiro_crew.mcp_dashboard import TABLE
from kiro_crew.mcp_tools.dashboard_client import (
    DashboardError,
    DashboardRequest,
    InMemoryDashboardClient,
)
from kiro_crew.mcp_tools.table import Caller, ToolContext

_LOST = {"error": "timed out", "transport_error": True}
_SEND = "POST /api/session-control/send"
_CREATE = "POST /api/session-control/create"
_CALLER = "dashboard:chat-1-100"


@pytest.fixture(autouse=True)
def _fresh_replays(_floor_monkeypatch: pytest.MonkeyPatch):
    _floor_monkeypatch.setattr(mcp_session_once, "LOST_REPLY_PAUSE_SECS", 0.0)
    session_control_replay.reset_for_tests()
    yield
    session_control_replay.reset_for_tests()


@pytest.fixture
def _admitted_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    """For tests of the replay table alone, driven with no gateway state: every
    replay passes the caller gate, which the route tests below pin."""

    async def _admit(_request, _op, _body):
        return None

    monkeypatch.setattr(session_control_replay, "_readmit", _admit)


def test_the_tool_and_the_gateway_name_the_same_field() -> None:
    assert mcp_session_once._REQUEST_ID_FIELD == session_control_replay.REQUEST_ID_FIELD


# ── The tools ────────────────────────────────────────────────────────────────


def _lost_then(reply: dict[str, Any], *, lost: int = 1, echo: bool = True):
    """A route that loses its first *lost* replies, then answers *reply*."""
    calls = {"n": 0}

    def _answer(req: DashboardRequest) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] <= lost:
            return _LOST
        if not echo:
            return dict(reply)
        return {
            **reply,
            "request_id": req.body["request_id"],
            "replay": "replayed",
            "replay_attempt": 0,
        }

    return _answer


def _call(tool: str, args: dict[str, Any], routes: dict[str, Any]):
    dash = InMemoryDashboardClient(routes)
    out = TABLE.call(tool, args, ToolContext(dash, Caller.strict(_CALLER)))
    return out, dash


def test_a_slow_send_is_resent_under_one_id_and_reports_the_delivery() -> None:
    delivered = {"ok": True, "target": "chat-2", "started": True, "steered": False}
    out, dash = _call(
        "session_send", {"target": "chat-2", "message": "go"}, {_SEND: _lost_then(delivered)}
    )
    first, second = dash.sent(_SEND)
    assert first.body.pop("request_attempt") == 0 and second.body.pop("request_attempt") == 1
    assert first.body == second.body
    assert len(first.body["request_id"]) == 32
    assert "Delivered to `chat-2`" in out
    assert "twice" not in out


def test_a_slow_create_is_resent_under_one_id_and_reports_the_session() -> None:
    opened = {"ok": True, "target": "chat-9", "title": "worker"}
    out, dash = _call("session_create", {"title": "worker"}, {_CREATE: _lost_then(opened)})
    first, second = dash.sent(_CREATE)
    assert first.body.pop("request_attempt") == 0 and second.body.pop("request_attempt") == 1
    assert first.body == second.body
    assert "Opened `chat-9`" in out
    assert "second session" not in out


def test_a_send_lost_on_every_try_says_not_to_send_again() -> None:
    out, dash = _call("session_send", {"target": "chat-2", "message": "go"}, {_SEND: _LOST})
    assert len(dash.sent(_SEND)) == mcp_session_once.LOST_REPLY_ATTEMPTS
    assert len({r.body["request_id"] for r in dash.sent(_SEND)}) == 1
    assert "may still have been delivered" in out
    assert "Do NOT send it again" in out


def test_a_create_lost_on_every_try_says_not_to_create_again() -> None:
    out, dash = _call("session_create", {"title": "worker"}, {_CREATE: _LOST})
    assert len(dash.sent(_CREATE)) == mcp_session_once.LOST_REPLY_ATTEMPTS
    assert "may still have been opened" in out
    assert "Do NOT create it again" in out


def test_a_refused_send_is_not_resent() -> None:
    refused = {"error": "not yours", "code": "not_creator"}
    out, dash = _call("session_send", {"target": "chat-2", "message": "go"}, {_SEND: refused})
    assert len(dash.sent(_SEND)) == 1
    assert out == "Error: could not send to that session: not yours"


def test_a_resend_an_older_gateway_ran_again_is_reported_as_a_possible_double() -> None:
    delivered = {"ok": True, "target": "chat-2", "started": True, "steered": False}
    out, _ = _call(
        "session_send",
        {"target": "chat-2", "message": "go"},
        {_SEND: _lost_then(delivered, echo=False)},
    )
    assert "may have been delivered twice" in out


def test_a_create_an_older_gateway_ran_again_is_reported_as_a_possible_double() -> None:
    opened = {"ok": True, "target": "chat-9", "title": "worker"}
    out, _ = _call("session_create", {"title": "worker"}, {_CREATE: _lost_then(opened, echo=False)})
    assert "a second session may have been opened" in out


# ── The routes ───────────────────────────────────────────────────────────────


def _request(state, caller, path: str, body: dict[str, Any], session_key: str = "") -> MagicMock:
    request = MagicMock()
    request.app = {"state": state}
    request.path = path
    request.method = "POST"
    request.headers = {"X-Session-Key": session_key or _key(caller)}
    request.get = lambda key, default=None: (
        True if key in ("internal_auth", "peer_verified") else default
    )

    async def _json():
        return body

    request.json = _json
    return request


def _replays(first, resent) -> bool:
    """Whether *resent* is *first*'s reply answered again from the replay table."""
    status, body = _reply(first)
    resent_status, resent_body = _reply(resent)
    return (
        resent_body.pop("replay") == "replayed"
        and isinstance(resent_body.pop("replay_attempt"), int)
        and body.pop("replay") in ("first", "unsaved")
        and (status, body) == (resent_status, resent_body)
    )


def _reply(response) -> tuple[int, dict[str, Any]]:
    return response.status, json.loads(response.body.decode())


@pytest.fixture
def turns(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    started: list[str] = []

    async def _fake_run_chat(_state, slot, _prompt):
        started.append(slot.key)

    monkeypatch.setattr("kiro_crew.dashboard.chat_runner._run_chat", _fake_run_chat)
    return started


def _send_state(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _peer_target(state, "chat-2", caller)
    return state, caller


_ID = "0123456789abcdef0123456789abcdef"
_SEND_BODY = {"target": "chat-2", "message": "do it", "request_id": _ID}


def test_a_send_resent_after_its_reply_was_lost_delivers_once(tmp_path, turns) -> None:
    state, caller = _send_state(tmp_path)
    path = "/api/session-control/send"

    async def _go():
        first = await handlers_sc.api_session_control_send(
            _request(state, caller, path, dict(_SEND_BODY))
        )
        resent = await handlers_sc.api_session_control_send(
            _request(state, caller, path, dict(_SEND_BODY))
        )
        return first, resent

    first, resent = asyncio.run(_go())
    assert _replays(first, resent)
    status, body = _reply(first)
    assert status == 200, body
    assert body["ok"] is True and body["request_id"] == _ID
    assert turns == ["chat-2"]


def test_a_send_resent_while_the_first_is_running_waits_for_it(tmp_path, monkeypatch) -> None:
    state, caller = _send_state(tmp_path)
    path = "/api/session-control/send"
    deliveries: list[str] = []

    async def _go():
        entered, release = asyncio.Event(), asyncio.Event()

        async def _slow_send(_state, *, target, **_kw):
            deliveries.append(target)
            entered.set()
            await release.wait()
            return {"ok": True, "target": target, "started": True, "steered": False}

        monkeypatch.setattr(sc, "send_to_target", _slow_send)
        first = asyncio.create_task(
            handlers_sc.api_session_control_send(_request(state, caller, path, dict(_SEND_BODY)))
        )
        await asyncio.wait_for(entered.wait(), timeout=30)
        resent = asyncio.create_task(
            handlers_sc.api_session_control_send(_request(state, caller, path, dict(_SEND_BODY)))
        )
        # Nothing here does I/O, so these yields carry the resend as far as it can go
        # while the first request is held: a resend that ran the work would add a
        # second delivery here, before the first one finishes.
        for _ in range(50):
            await asyncio.sleep(0)
        assert not resent.done()
        assert deliveries == ["chat-2"]
        release.set()
        return await asyncio.wait_for(asyncio.gather(first, resent), timeout=30)

    first, resent = asyncio.run(_go())
    assert deliveries == ["chat-2"]
    assert _replays(first, resent)
    assert _reply(resent)[1]["request_id"] == _ID


def test_a_create_resent_after_its_reply_was_lost_opens_one_session(tmp_path) -> None:
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    path = "/api/session-control/create"
    body = {"title": "worker", "request_id": _ID}
    before = state.live_slot_count()

    async def _go():
        first = await handlers_sc.api_session_control_create(_request(state, caller, path, body))
        resent = await handlers_sc.api_session_control_create(_request(state, caller, path, body))
        return first, resent

    first, resent = asyncio.run(_go())
    assert _replays(first, resent)
    status, payload = _reply(first)
    assert status == 200, payload
    assert payload["request_id"] == _ID
    assert state.live_slot_count() == before + 1


@pytest.mark.parametrize(
    ("route", "verb", "path", "body"),
    [
        pytest.param(
            handlers_sc.api_session_control_send,
            "send_to_target",
            "/api/session-control/send",
            {"target": "chat-2", "message": "go"},
            id="send",
        ),
        pytest.param(
            handlers_sc.api_session_control_create,
            "create_session",
            "/api/session-control/create",
            {"title": "worker"},
            id="create",
        ),
    ],
)
def test_a_replay_passes_the_callers_gate_again(
    tmp_path, monkeypatch, route, verb, path, body
) -> None:
    """A kept reply reaches the caller only while the caller may still have it."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    runs: list[dict[str, Any]] = []
    admitted: list[Any] = []

    async def _verb(_state, **kwargs):
        runs.append(kwargs)
        return {"ok": True, "target": "chat-9", "title": "private"}

    monkeypatch.setattr(sc, verb, _verb)
    keyed = {**body, "request_id": _ID, "request_attempt": 0}

    async def _go():
        first = await route(_request(state, caller, path, dict(keyed)))
        monkeypatch.setattr(
            sc, "record_audience_admission", lambda slot, admission: admitted.append(slot)
        )
        replayed = await route(_request(state, caller, path, {**keyed, "request_attempt": 1}))
        # The caller becomes app-scoped, which the caller gate refuses.
        caller._app = "some-app"
        refused = await route(_request(state, caller, path, {**keyed, "request_attempt": 2}))
        return first, replayed, refused

    first, replayed, refused = (_reply(r) for r in asyncio.run(_go()))
    assert first[0] == 200 and len(runs) == 1, first
    assert replayed[1]["replay"] == "replayed" and admitted == [caller]
    assert (refused[0], refused[1]["code"]) == (403, "app_scoped_caller")
    assert "replay" not in refused[1] and "title" not in refused[1]


def test_a_reused_id_with_another_body_is_refused(tmp_path, turns) -> None:
    state, caller = _send_state(tmp_path)
    path = "/api/session-control/send"

    async def _go():
        await handlers_sc.api_session_control_send(_request(state, caller, path, dict(_SEND_BODY)))
        other = {**_SEND_BODY, "message": "something else"}
        return await handlers_sc.api_session_control_send(_request(state, caller, path, other))

    status, body = _reply(asyncio.run(_go()))
    assert (status, body["code"]) == (409, "request_id_reused")
    assert turns == ["chat-2"]


def test_a_malformed_id_is_refused_before_anything_is_sent(tmp_path, turns) -> None:
    state, caller = _send_state(tmp_path)
    request = _request(
        state, caller, "/api/session-control/send", {**_SEND_BODY, "request_id": "x"}
    )
    status, body = _reply(asyncio.run(handlers_sc.api_session_control_send(request)))
    assert (status, body["code"]) == (400, "invalid_request_id")
    assert turns == []


def test_a_send_without_an_id_runs_every_time(tmp_path, turns) -> None:
    state, caller = _send_state(tmp_path)
    path = "/api/session-control/send"
    plain = {"target": "chat-2", "message": "do it"}

    async def _go():
        for _ in range(2):
            await handlers_sc.api_session_control_send(_request(state, caller, path, dict(plain)))

    asyncio.run(_go())
    assert turns == ["chat-2", "chat-2"]


def test_another_callers_id_is_not_answered_from_this_callers_reply() -> None:
    from aiohttp import web

    runs: list[str] = []

    def _run_as(who: str):
        async def _run() -> web.Response:
            runs.append(who)
            return web.json_response({"ok": True, "who": who})

        return _run

    async def _go():
        replies = []
        for who in ("dashboard:chat-1", "dashboard:chat-3"):
            request = _request(None, None, "/api/session-control/send", dict(_SEND_BODY), who)
            replies.append(await session_control_replay.once(request, "send", _run_as(who)))
        return replies

    first, second = asyncio.run(_go())
    assert runs == ["dashboard:chat-1", "dashboard:chat-3"]
    assert _reply(second)[1]["who"] == "dashboard:chat-3"


@pytest.mark.usefixtures("_admitted_replay")
def test_a_resend_after_the_first_attempt_crashed_is_not_run_again() -> None:
    """The work may have happened before the crash, so the resend must not repeat it."""
    runs: list[int] = []

    async def _crash():
        runs.append(1)
        raise RuntimeError("boom")

    async def _go():
        request = _request(None, None, "/api/session-control/send", dict(_SEND_BODY), _CALLER)
        with pytest.raises(RuntimeError):
            await session_control_replay.once(request, "send", _crash)
        return await session_control_replay.once(request, "send", _crash)

    status, body = _reply(asyncio.run(_go()))
    assert runs == [1]
    assert (status, body["code"]) == (500, "replay_outcome_unknown")


def _kept_send(request_id: str = _ID) -> MagicMock:
    body = {**_SEND_BODY, "request_id": request_id}
    return _request(None, None, "/api/session-control/send", body, _CALLER)


@pytest.mark.usefixtures("_admitted_replay")
def test_the_size_cap_never_drops_a_request_that_is_still_running(_floor_monkeypatch) -> None:
    """Dropping a running reply would let its resend run the work a second time."""
    from aiohttp import web

    _floor_monkeypatch.setattr(session_control_replay, "MAX_KEPT_REPLIES", 1)
    runs: list[int] = []

    async def _go():
        release = asyncio.Event()

        async def _held() -> web.Response:
            runs.append(1)
            await release.wait()
            return web.json_response({"ok": True})

        first = asyncio.create_task(session_control_replay.once(_kept_send(), "send", _held))
        resent = asyncio.create_task(session_control_replay.once(_kept_send(), "send", _held))
        for _ in range(50):
            await asyncio.sleep(0)
        release.set()
        return await asyncio.wait_for(asyncio.gather(first, resent), timeout=30)

    first, resent = asyncio.run(_go())
    assert runs == [1]
    assert _replays(first, resent)


def _run_as(runs: list[str], label: str):
    from aiohttp import web

    async def _run() -> web.Response:
        runs.append(label)
        return web.json_response({"ok": True, "who": label})

    return _run


@pytest.mark.usefixtures("_admitted_replay")
def test_at_capacity_a_new_id_runs_unkept_and_a_kept_reply_still_answers(
    _floor_monkeypatch,
) -> None:
    """A full table refuses nobody, and never pushes out a reply whose resend is to come."""
    _floor_monkeypatch.setattr(session_control_replay, "MAX_KEPT_REPLIES", 1)
    runs: list[str] = []

    async def _go():
        first = await session_control_replay.once(_kept_send(), "send", _run_as(runs, "first"))
        other = await session_control_replay.once(
            _kept_send("f" * 32), "send", _run_as(runs, "other")
        )
        resent = await session_control_replay.once(_kept_send(), "send", _run_as(runs, "resent"))
        return first, other, resent

    first, other, resent = asyncio.run(_go())
    assert runs == ["first", "other"]
    status, body = _reply(other)
    assert status == 200 and body["replay"] == "unsaved"
    assert _replays(first, resent)


@pytest.mark.usefixtures("_admitted_replay")
def test_one_caller_at_its_limit_leaves_other_callers_protected(_floor_monkeypatch) -> None:
    _floor_monkeypatch.setattr(session_control_replay, "MAX_KEPT_REPLIES_PER_CALLER", 1)
    runs: list[str] = []

    def _send_as(caller: str, request_id: str) -> MagicMock:
        body = {**_SEND_BODY, "request_id": request_id}
        return _request(None, None, "/api/session-control/send", body, caller)

    async def _go():
        busy, other = "dashboard:chat-busy", "dashboard:chat-other"
        await session_control_replay.once(_send_as(busy, _ID), "send", _run_as(runs, "busy-1"))
        unkept = await session_control_replay.once(
            _send_as(busy, "e" * 32), "send", _run_as(runs, "busy-2")
        )
        kept = await session_control_replay.once(
            _send_as(other, _ID), "send", _run_as(runs, "other")
        )
        resent = await session_control_replay.once(
            _send_as(other, _ID), "send", _run_as(runs, "other-again")
        )
        return unkept, kept, resent

    unkept, kept, resent = asyncio.run(_go())
    assert runs == ["busy-1", "busy-2", "other"]
    assert _reply(unkept)[1]["replay"] == "unsaved"
    assert _reply(kept)[1]["request_id"] == _ID
    assert _replays(kept, resent)


def test_the_tool_and_the_gateway_name_the_same_unknown_outcome_codes() -> None:
    assert mcp_session_once._REPLAY_UNKNOWN_CODES == {
        session_control_replay.OUTCOME_UNKNOWN_CODE,
        session_control_replay.REPLY_TOO_LARGE_CODE,
    }


def _lost_then_refused(refusal: dict[str, Any], *, echo: bool):
    """A route that loses its first reply, then refuses the resend."""
    calls = {"n": 0}

    def _answer(req: DashboardRequest) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            return _LOST
        if not echo:
            return dict(refusal)
        return {
            **refusal,
            "request_id": req.body["request_id"],
            "replay": "replayed",
            "replay_attempt": 0,
        }

    return _answer


def test_a_resend_an_older_gateway_refused_keeps_the_send_an_unknown_outcome() -> None:
    """The refusal is the resend's own; the first post may still have delivered."""
    refused = {"error": "busy", "code": "target_busy"}
    out, _ = _call(
        "session_send",
        {"target": "chat-2", "message": "go"},
        {_SEND: _lost_then_refused(refused, echo=False)},
    )
    assert "may still have been delivered" in out
    assert "could not send" not in out


def test_a_resend_an_older_gateway_refused_keeps_the_create_an_unknown_outcome() -> None:
    refused = {"error": "cap reached", "code": "bot_worker_cap_reached"}
    out, _ = _call(
        "session_create", {"title": "worker"}, {_CREATE: _lost_then_refused(refused, echo=False)}
    )
    assert "may still have been opened" in out
    assert "could not create" not in out


def test_a_replayed_first_refusal_is_reported_as_the_refusal() -> None:
    """The gateway echoed the id, so the refusal is the first post's own outcome."""
    refused = {"error": "not yours", "code": "not_creator"}
    out, _ = _call(
        "session_send",
        {"target": "chat-2", "message": "go"},
        {_SEND: _lost_then_refused(refused, echo=True)},
    )
    assert out == "Error: could not send to that session: not yours"


@pytest.mark.parametrize(
    "code",
    [session_control_replay.OUTCOME_UNKNOWN_CODE, session_control_replay.REPLY_TOO_LARGE_CODE],
)
def test_a_replay_that_cannot_show_the_first_outcome_stays_unknown(code: str) -> None:
    out, _ = _call(
        "session_send",
        {"target": "chat-2", "message": "go"},
        {_SEND: _lost_then_refused({"error": "unknown", "code": code}, echo=True)},
    )
    assert "may still have been delivered" in out


@pytest.mark.usefixtures("_admitted_replay")
def test_a_reply_too_long_to_keep_still_stops_a_resend(_floor_monkeypatch) -> None:
    """The first caller gets its whole reply; a resend gets a short marker, not a rerun."""
    from aiohttp import web

    _floor_monkeypatch.setattr(session_control_replay, "MAX_KEPT_REPLY_CHARS", 100)
    runs: list[int] = []

    async def _long() -> web.Response:
        runs.append(1)
        return web.json_response({"error": "x" * 500, "code": "target_not_found"}, status=404)

    async def _go():
        def _req():
            return _request(None, None, "/api/session-control/send", dict(_SEND_BODY), _CALLER)

        first = await session_control_replay.once(_req(), "send", _long)
        resent = await session_control_replay.once(_req(), "send", _long)
        return first, resent

    first, resent = asyncio.run(_go())
    assert runs == [1]
    assert _reply(first)[0] == 404 and len(_reply(first)[1]["error"]) == 500
    status, body = _reply(resent)
    assert (status, body["code"]) == (409, session_control_replay.REPLY_TOO_LARGE_CODE)
    assert len(resent.text) < 400


def _decoded_refusal(body: dict[str, Any]) -> DashboardError:
    """A refusal as the production client decodes it from the gateway's HTTP reply."""
    import io
    import urllib.error

    from kiro_crew.mcp_core_parts.gateway import _http_error_body

    raw = io.BytesIO(json.dumps(body).encode())
    exc = urllib.error.HTTPError("http://127.0.0.1/x", body.get("status", 409), "", {}, raw)
    return DashboardError(_http_error_body(exc))


def test_a_replayed_refusal_keeps_its_request_id_through_the_http_decoder() -> None:
    refused = _decoded_refusal(
        {
            "error": "not yours",
            "code": "not_creator",
            "request_id": _ID,
            "replay": "replayed",
            "replay_attempt": 0,
        }
    )
    assert mcp_session_once._replays_first_outcome(refused, _ID)


def test_the_http_decoder_drops_a_request_id_of_another_shape() -> None:
    refused = _decoded_refusal({"error": "no", "code": "not_creator", "request_id": "X" * 40})
    assert "request_id" not in refused.body
    assert not mcp_session_once._replays_first_outcome(refused, _ID)


def test_the_tool_and_the_gateway_name_the_same_replay_marker() -> None:
    assert mcp_session_once._REPLAY_FIELD == session_control_replay.REPLAY_FIELD
    assert mcp_session_once._REPLAYED == session_control_replay.REPLAY_REPLAYED


@pytest.mark.parametrize("replay", ["first", "unsaved"])
def test_a_resend_the_gateway_ran_again_is_never_reported_as_clean(replay: str) -> None:
    """The id is echoed, but the resend ran as new work (restart, expiry or full table)."""
    calls = {"n": 0}

    def _answer(req: DashboardRequest) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            return _LOST
        return {
            "ok": True,
            "target": "chat-2",
            "started": True,
            "steered": False,
            "request_id": req.body["request_id"],
            "replay": replay,
        }

    out, _ = _call("session_send", {"target": "chat-2", "message": "go"}, {_SEND: _answer})
    assert "may have been delivered twice" in out


def test_a_resend_refused_after_running_again_stays_an_unknown_outcome() -> None:
    """A fresh refusal of the resend says nothing about the lost first post."""
    calls = {"n": 0}

    def _answer(req: DashboardRequest) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            return _LOST
        return {
            "error": "cap reached",
            "code": "bot_worker_cap_reached",
            "request_id": req.body["request_id"],
            "replay": "first",
        }

    out, _ = _call("session_create", {"title": "worker"}, {_CREATE: _answer})
    assert "may still have been opened" in out


def _runs_twice_after(between) -> list:
    """Run one keyed send, call *between*, resend it; return both replies and the runs."""
    from aiohttp import web

    runs: list[int] = []

    async def _run() -> web.Response:
        runs.append(1)
        return web.json_response({"ok": True})

    async def _go():
        first = await session_control_replay.once(_kept_send(), "send", _run)
        between()
        resent = await session_control_replay.once(_kept_send(), "send", _run)
        return first, resent

    first, resent = asyncio.run(_go())
    return [first, resent, runs]


def test_a_resend_after_a_gateway_restart_says_it_ran_again() -> None:
    first, resent, runs = _runs_twice_after(session_control_replay.reset_for_tests)
    assert runs == [1, 1]
    assert _reply(first)[1]["replay"] == "first"
    assert _reply(resent)[1]["replay"] == "first"


def test_a_resend_after_its_reply_expired_says_it_ran_again() -> None:
    def _age_past_the_window() -> None:
        for kept in session_control_replay._kept.values():
            assert kept.finished_at is not None
            kept.finished_at -= 2 * session_control_replay.REPLAY_TTL_SECS

    first, resent, runs = _runs_twice_after(_age_past_the_window)
    assert runs == [1, 1]
    assert _reply(resent)[1]["replay"] == "first"


def test_the_http_decoder_keeps_the_replay_marker_and_drops_unknown_values() -> None:
    kept = _decoded_refusal(
        {"error": "no", "code": "x", "request_id": _ID, "replay": "replayed", "replay_attempt": 0}
    )
    assert kept.body["replay"] == "replayed"
    dropped = _decoded_refusal({"error": "no", "code": "x", "request_id": _ID, "replay": "yes"})
    assert "replay" not in dropped.body


def test_the_tool_and_the_gateway_name_the_same_attempt_fields() -> None:
    assert mcp_session_once._ATTEMPT_FIELD == session_control_replay.ATTEMPT_FIELD
    assert mcp_session_once._REPLAY_ATTEMPT_FIELD == session_control_replay.REPLAY_ATTEMPT_FIELD


def test_a_replay_of_a_later_post_is_not_proof_the_first_did_not_run() -> None:
    """Posts 1 and 2 both lose their replies; post 3 is answered from post 2's kept reply.

    Post 1 may have run on a gateway that has since restarted, so the call must warn.
    """
    calls = {"n": 0}

    def _answer(req: DashboardRequest) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] < 3:
            return _LOST
        return {
            "ok": True,
            "target": "chat-2",
            "started": True,
            "steered": False,
            "request_id": req.body["request_id"],
            "replay": "replayed",
            "replay_attempt": 1,
        }

    out, dash = _call("session_send", {"target": "chat-2", "message": "go"}, {_SEND: _answer})
    assert [r.body["request_attempt"] for r in dash.sent(_SEND)] == [0, 1, 2]
    assert "may have been delivered twice" in out


@pytest.mark.usefixtures("_admitted_replay")
def test_a_replay_names_the_post_whose_reply_it_carries() -> None:
    from aiohttp import web

    runs: list[int] = []

    async def _run() -> web.Response:
        runs.append(1)
        return web.json_response({"ok": True})

    def _post(attempt: int) -> MagicMock:
        body = {**_SEND_BODY, "request_attempt": attempt}
        return _request(None, None, "/api/session-control/send", body, _CALLER)

    async def _go():
        first = await session_control_replay.once(_post(1), "send", _run)
        resent = await session_control_replay.once(_post(2), "send", _run)
        return first, resent

    first, resent = asyncio.run(_go())
    assert runs == [1]
    assert _reply(resent)[1]["replay_attempt"] == 1
    assert "replay_attempt" not in _reply(first)[1]


def test_the_http_decoder_keeps_the_replay_attempt() -> None:
    refused = _decoded_refusal(
        {"error": "no", "code": "x", "request_id": _ID, "replay": "replayed", "replay_attempt": 0}
    )
    assert refused.body["replay_attempt"] == 0
    odd = _decoded_refusal({"error": "no", "code": "x", "replay_attempt": True})
    assert "replay_attempt" not in odd.body
