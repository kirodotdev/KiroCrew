"""The gateway's timed Do Not Disturb, applied at the notification delivery sink.

While it runs, a note that is not urgent is delivered like a muted channel's
(``silenced`` and passive, so no badge, banner, push or bridge delivery); a
critical note and an approval still interrupt. It ends on its own once the
clock passes the stored end time.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers.messaging import (
    api_notification_dnd_get,
    api_notification_dnd_put,
)
from kiro_crew.dashboard.notification_coordinator import unread_count
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.notifications.bus import NotificationPayload
from kiro_crew.notifications.dnd import MAX_DND_SECS, DndError, DoNotDisturb

NOW = 1_800_000_000.0


class _Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.notifications.settings.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.notifications.dnd.config_dir", lambda: tmp_path)
    return tmp_path


def _state(clock: _Clock) -> DashboardState:
    state = DashboardState(
        sessions=MagicMock(count=0), crons=MagicMock(), lessons=MagicMock(), start_time=0.0
    )
    state.notification_dnd = DoNotDisturb(clock)
    return state


def _publish(state: DashboardState, **over) -> dict:
    fields = dict(source="system", channel="system.agent", title="Build finished", body="42 rows")
    fields.update(over)
    state.notification_bus.push(NotificationPayload(**fields))
    return state._notification_log[-1]


class TestSink:
    def test_a_note_is_silenced_while_dnd_runs(self, home):
        clock = _Clock()
        state = _state(clock)
        state.notification_dnd.set(NOW + 3600)
        note = _publish(state)
        assert note["silenced"] is True
        assert note["priority"] == "passive"
        assert unread_count(state._notification_log) == 0

    def test_without_dnd_a_note_interrupts(self, home):
        state = _state(_Clock())
        note = _publish(state)
        assert "silenced" not in note
        assert unread_count(state._notification_log) == 1

    @pytest.mark.parametrize(
        "over",
        [{"priority": "critical"}, {"channel": "system.approval", "priority": "critical"}],
        ids=["critical", "approval"],
    )
    def test_urgent_notes_are_exempt(self, home, over):
        state = _state(_Clock())
        state.notification_dnd.set(NOW + 3600)
        note = _publish(state, **over)
        assert "silenced" not in note
        assert note["priority"] == "critical"
        assert unread_count(state._notification_log) == 1

    def test_notes_interrupt_again_once_the_end_time_passes(self, home):
        clock = _Clock()
        state = _state(clock)
        state.notification_dnd.set(NOW + 3600)
        assert _publish(state).get("silenced") is True
        clock.now = NOW + 3600
        assert "silenced" not in _publish(state)
        assert state.notification_dnd.until() is None

    def test_a_silenced_note_is_not_sent_to_web_push(self, home):
        state = _state(_Clock())
        sent: list[dict] = []
        state._notification_coordinator._web_push_fanout = sent.append
        _publish(state, title="before")
        state.notification_dnd.set(NOW + 3600)
        _publish(state, title="during")
        assert [note["title"] for note in sent] == ["before"]


class TestStoredEndTime:
    def test_survives_a_restart(self, home):
        DoNotDisturb(_Clock()).set(NOW + 600)
        assert DoNotDisturb(_Clock()).until() == NOW + 600

    def test_an_expired_end_time_reads_as_off(self, home):
        DoNotDisturb(_Clock()).set(NOW + 600)
        assert DoNotDisturb(_Clock(NOW + 601)).suppresses() is False

    def test_an_end_time_past_the_horizon_reads_as_off(self, home):
        # The clock was moved back after DND started: the stored end time is now
        # further ahead than any choice offers, so notifications go through.
        DoNotDisturb(_Clock()).set(NOW + 3600)
        assert DoNotDisturb(_Clock(NOW + 3600 - MAX_DND_SECS - 1)).suppresses() is False

    @pytest.mark.parametrize(
        "raw",
        [
            '{"dnd_until": "soon"}',
            '{"dnd_until": true}',
            "[1]",
            "not json",
            # An int too large for a float: math.isfinite raises OverflowError on it.
            pytest.param('{"dnd_until": 1' + "0" * 400 + "}", id="int-too-large-for-float"),
        ],
    )
    def test_a_malformed_file_reads_as_off(self, home, raw):
        (home / "notification_dnd.json").write_text(raw, encoding="utf-8")
        assert DoNotDisturb(_Clock()).suppresses() is False

    @pytest.mark.parametrize(
        "until",
        [
            "soon",
            True,
            float("nan"),
            float("inf"),
            NOW,
            NOW - 1,
            NOW + MAX_DND_SECS + 1,
            pytest.param(10**400, id="int-too-large-for-float"),
        ],
    )
    def test_set_refuses_a_bad_end_time_and_keeps_the_old_one(self, home, until):
        dnd = DoNotDisturb(_Clock())
        dnd.set(NOW + 60)
        with pytest.raises(DndError):
            dnd.set(until)
        assert dnd.until() == NOW + 60

    def test_set_none_ends_it(self, home):
        dnd = DoNotDisturb(_Clock())
        dnd.set(NOW + 60)
        assert dnd.set(None) is None
        assert dnd.suppresses() is False
        assert json.loads((home / "notification_dnd.json").read_text()) == {"dnd_until": None}


def _app(state) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/notifications/dnd", api_notification_dnd_get)
    app.router.add_put("/api/notifications/dnd", api_notification_dnd_put)
    return app


class TestApi:
    @pytest.mark.asyncio
    async def test_put_then_get_and_broadcast(self, home):
        state = _state(_Clock())
        frames: list[tuple] = []
        state.broadcast_ws = lambda kind, data: frames.append((kind, data))
        async with TestClient(TestServer(_app(state))) as client:
            put = await client.put("/api/notifications/dnd", json={"until": NOW + 7200})
            got = await client.get("/api/notifications/dnd")
            assert put.status == 200
            assert await got.json() == {"until": NOW + 7200}
            cleared = await client.put("/api/notifications/dnd", json={"until": None})
            assert await cleared.json() == {"until": None}
        assert frames == [
            ("notification_dnd", {"until": NOW + 7200}),
            ("notification_dnd", {"until": None}),
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("body", [{"until": NOW - 5}, {"until": "x"}, {}, [1]])
    async def test_put_refuses_a_bad_body(self, home, body):
        state = _state(_Clock())
        async with TestClient(TestServer(_app(state))) as client:
            resp = await client.put("/api/notifications/dnd", json=body)
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_until"
        assert state.notification_dnd.until() is None

    @pytest.mark.asyncio
    async def test_put_refuses_an_int_too_large_for_a_float(self, home):
        state = _state(_Clock())
        body = '{"until": 1' + "0" * 400 + "}"
        async with TestClient(TestServer(_app(state))) as client:
            resp = await client.put(
                "/api/notifications/dnd",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_until"
        assert state.notification_dnd.until() is None
