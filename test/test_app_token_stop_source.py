"""App-token stops are audited as ``via: app:<app>``, not as the owner's press.

``POST /api/chat/slots/{slot}/stop`` is the layer holding the request an app
token rides on (#20874): ``stop_slot_turn`` defaults ``source`` to ``"dashboard"``
and writes it into every ``dashboard_stop`` SEL line it logs as ``metadata.via``,
so a route that omits ``source=`` audits an app's soft stop, its ``?force=true``
escape and its escalated second press exactly like the owner pressing Stop in
the dashboard. Since #19898 the embedded chat sends app-token stops for real,
which turned that default into a live audit blind spot.

The tests drive the routed handler through a real aiohttp app whose middleware
publishes the identity the deny checks already read, and pin three things:

* an app token's soft stop, force escape and escalated second press all record
  ``metadata.via == "app:<app>"``, while the owner's press still records
  ``"dashboard"``;
* both deny checks still run, in order, before any side effect — the guard
  placement the fix must not disturb;
* ``stop_slot_turn``'s own contract: the ``source`` it is handed is the ``via``
  it logs, which is what makes the route's one argument carry the caller.

The audit EVENT's own ``source`` field stays ``"dashboard"`` on these lines: it
names the client surface (the dashboard stop API), while ``metadata.via`` names
who asked — the decision recorded on the issue is that only ``via`` follows the
caller.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.chat_handlers import api_chat_slot_stop, stop_slot_turn

_TEST_DIR = str(Path(__file__).resolve().parent)

SLOT = "chat-20874"
KEY = "dashboard:" + SLOT


def _dashboard_state(tmp_path):
    if _TEST_DIR not in sys.path:
        sys.path.insert(0, _TEST_DIR)
    from chat_test_helpers import _make_state

    return _make_state(tmp_path)


def _running(slot):
    """The slot a live turn is running on (``running`` reads ``task.done()``)."""
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    return slot


def _stop_lines(lines: list[dict]) -> list[dict]:
    return [kw for kw in lines if kw.get("tool_name") == "dashboard_stop"]


def _fences(monkeypatch, state, *, events: list, compacting: bool = False) -> list[dict]:
    """Allow the two deny checks through (recording them) and capture the SEL lines.

    ``deny_app_slot_access`` is the ONE fence both checks route through: the
    route's own slot check and the session-ownership check inside the real
    ``_app_cancel_denied`` — which is deliberately left unpatched so the tests
    exercise the authorization order as shipped.
    """
    state.sessions.is_compacting = MagicMock(return_value=compacting)

    async def _stop_turn(*_args, **kwargs):
        events.append(("stop", kwargs.get("force")))
        return "hard" if kwargs.get("force") else "soft"

    state.sessions.stop_turn = AsyncMock(side_effect=_stop_turn)

    def _deny(request_app, slot, name, operation):
        events.append(("deny", operation, request_app))
        return None

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.deny_app_slot_access", _deny)
    # One session, resolved once: what the route authorizes is what it cancels.
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.effective_session_key", lambda slot: KEY)
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.own_session_key", lambda slot: KEY)

    lines: list[dict] = []

    def _sel():
        sel = MagicMock()
        sel.log_tool_invocation.side_effect = lambda **kwargs: lines.append(kwargs)
        return sel

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", _sel)
    return lines


def _wire_route(state, *, app_name: str) -> web.Application:
    """The stop route behind the middleware that publishes the caller's identity."""
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def _publish_identity(request: web.Request, handler):
        request["app"] = app_name
        return await handler(request)

    app.middlewares.append(_publish_identity)
    app.router.add_post("/api/chat/slots/{slot}/stop", api_chat_slot_stop)
    return app


async def _press(client: TestClient, *, force: bool = False):
    url = f"/api/chat/slots/{SLOT}/stop" + ("?force=true" if force else "")
    resp = await client.post(url)
    return resp.status, await resp.json()


class TestAppTokenStopVia:
    @pytest.mark.asyncio
    async def test_app_token_soft_stop_is_audited_as_app(self, tmp_path, monkeypatch) -> None:
        state = _dashboard_state(tmp_path)
        _running(state.get_or_create_slot(SLOT))
        events: list = []
        lines = _fences(monkeypatch, state, events=events)
        async with TestClient(TestServer(_wire_route(state, app_name="mail-bot"))) as client:
            status, body = await _press(client)

        assert status == 200
        assert body == {"ok": True}
        (line,) = _stop_lines(lines)
        assert line["metadata"]["via"] == "app:mail-bot"
        assert line["metadata"]["force"] is False
        # The event's own source keeps the client-surface name; via carries the caller.
        assert line["source"] == "dashboard"

    @pytest.mark.asyncio
    async def test_dashboard_stop_still_audits_as_dashboard(self, tmp_path, monkeypatch) -> None:
        state = _dashboard_state(tmp_path)
        _running(state.get_or_create_slot(SLOT))
        events: list = []
        lines = _fences(monkeypatch, state, events=events)
        async with TestClient(TestServer(_wire_route(state, app_name=""))) as client:
            status, body = await _press(client)

        assert status == 200
        assert body == {"ok": True}
        (line,) = _stop_lines(lines)
        assert line["metadata"]["via"] == "dashboard"

    @pytest.mark.asyncio
    async def test_app_token_force_escape_is_audited_as_app(self, tmp_path, monkeypatch) -> None:
        """``?force=true`` is the escape hatch from a compaction decline: the hard
        kill it triggers is still the app's, not the owner's."""
        state = _dashboard_state(tmp_path)
        _running(state.get_or_create_slot(SLOT))
        events: list = []
        lines = _fences(monkeypatch, state, events=events, compacting=True)
        async with TestClient(TestServer(_wire_route(state, app_name="mail-bot"))) as client:
            status, body = await _press(client, force=True)

        assert status == 200
        assert body == {"ok": True}
        (line,) = _stop_lines(lines)
        assert line["metadata"]["via"] == "app:mail-bot"
        assert line["metadata"]["force"] is True
        assert line["metadata"]["escalated"] is True
        assert line["outcome"] == "hard"

    @pytest.mark.asyncio
    async def test_app_token_escalated_second_press_is_audited_as_app(
        self, tmp_path, monkeypatch
    ) -> None:
        """A second press while the cooperative cancel is pending escalates to the
        hard kill; both presses carry the app, not the owner."""
        state = _dashboard_state(tmp_path)
        _running(state.get_or_create_slot(SLOT))
        events: list = []
        lines = _fences(monkeypatch, state, events=events)
        async with TestClient(TestServer(_wire_route(state, app_name="mail-bot"))) as client:
            first = await _press(client)
            second = await _press(client)

        assert first == (200, {"ok": True})
        assert second == (200, {"ok": True})
        assert [line["metadata"]["via"] for line in _stop_lines(lines)] == [
            "app:mail-bot",
            "app:mail-bot",
        ]
        assert _stop_lines(lines)[1]["metadata"]["escalated"] is True


class TestAuthorizationOrderUnchanged:
    @pytest.mark.asyncio
    async def test_both_denies_still_precede_any_side_effect(self, tmp_path, monkeypatch) -> None:
        state = _dashboard_state(tmp_path)
        _running(state.get_or_create_slot(SLOT))
        events: list = []
        lines = _fences(monkeypatch, state, events=events)
        async with TestClient(TestServer(_wire_route(state, app_name="mail-bot"))) as client:
            await _press(client)

        assert [event[1] for event in events if event[0] == "deny"] == [
            "slot_stop",
            "chat_stop",
        ]
        assert events[:3] == [
            ("deny", "slot_stop", "mail-bot"),
            ("deny", "chat_stop", "mail-bot"),
            ("stop", False),
        ]
        # And the audit line is written with the stop, never ahead of the guards.
        assert _stop_lines(lines)[0]["metadata"]["via"] == "app:mail-bot"


class TestStopSlotTurnSourceContract:
    @pytest.mark.asyncio
    async def test_the_source_handed_in_is_the_via_logged(self, tmp_path, monkeypatch) -> None:
        """The function-level contract the route's one argument relies on."""
        state = _dashboard_state(tmp_path)
        _running(state.get_or_create_slot(SLOT))
        events: list = []
        lines = _fences(monkeypatch, state, events=events)

        await stop_slot_turn(state, state._slots[SLOT], source="app:mail-bot", cancel_key=KEY)

        (line,) = _stop_lines(lines)
        assert line["metadata"]["via"] == "app:mail-bot"
