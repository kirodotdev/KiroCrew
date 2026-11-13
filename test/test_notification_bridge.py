"""The notification bridge's egress seam (RFC notification-bridge, phase B1a).

Pins the composite bus sink (local first, bridge after, a bridge failure never
reaching the producer), the dispatcher's rule match on the note's EFFECTIVE
priority, the route settings and their validation, and the loop invariant that
the bridge never publishes back onto the bus.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from real_dashboard_state import settle_notification_writes

from kiro_crew.dashboard.handlers.messaging import api_notification_channel_settings
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.notifications import bridge as bridge_mod
from kiro_crew.notifications import bridge_egress as bridge_egress_mod
from kiro_crew.notifications import bridge_slack as bridge_slack_mod
from kiro_crew.notifications import bridge_transport as bridge_transport_mod
from kiro_crew.notifications.bridge import (
    BRIDGE_TRANSPORTS,
    BridgeDispatcher,
    DeliveryRule,
    composite_sink,
    rule_from_settings,
)
from kiro_crew.notifications.bus import NotificationPayload
from kiro_crew.notifications.settings import (
    ChannelSettings,
    ChannelSettingsError,
    parse_imported_settings,
)


class _RecordingSink:
    def __init__(self, transport: str, fail: bool = False) -> None:
        self.transport = transport
        self.fail = fail
        self.notes: list[dict[str, Any]] = []

    async def deliver(self, note):
        self.notes.append(note)
        if self.fail:
            raise RuntimeError("transport down")


@pytest.fixture(autouse=True)
def _settle_writes():
    """Wait for queued notification appends before the conftest home pin is undone."""
    yield
    settle_notification_writes()


@pytest.fixture()
def settings(monkeypatch, tmp_path) -> ChannelSettings:
    monkeypatch.setattr("kiro_crew.notifications.settings.config_dir", lambda: tmp_path)
    return ChannelSettings()


def _make_state(monkeypatch, tmp_path) -> DashboardState:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.notifications.settings.config_dir", lambda: tmp_path)
    return DashboardState(
        sessions=MagicMock(count=0),
        crons=MagicMock(),
        lessons=MagicMock(),
        start_time=0.0,
    )


def _note(**fields: Any) -> dict[str, Any]:
    return {"ts": "t1", "channel": "system.cron", "priority": "critical", "title": "x", **fields}


class TestDeliveryRule:
    @pytest.mark.parametrize(
        ("floor", "admitted"),
        [
            ("critical", {"critical"}),
            ("default", {"critical", "default"}),
            ("all", {"critical", "default", "passive"}),
        ],
    )
    def test_each_floor_admits_its_priorities(self, floor, admitted):
        rule = DeliveryRule(transports=("slack",), min_priority=floor)
        assert {p for p in ("critical", "default", "passive") if rule.admits(p)} == admitted

    def test_route_defaults_to_the_critical_floor(self):
        assert rule_from_settings({"deliver_to": ["slack"]}) == DeliveryRule(("slack",), "critical")

    def test_hand_edited_unknowns_are_ignored_on_read(self):
        rule = rule_from_settings(
            {"deliver_to": ["slack", "carrier-pigeon", "slack"], "deliver_min_priority": "loud"}
        )
        assert rule == DeliveryRule(("slack",), "critical")

    @pytest.mark.parametrize("entry", [None, {}, {"muted": True}, {"deliver_to": "slack"}])
    def test_no_route_without_a_transport_list(self, entry):
        assert rule_from_settings(entry).transports == ()


class TestRouteSettings:
    def test_route_is_stored_deduplicated_in_order(self, settings):
        entry = settings.update("system.cron", deliver_to=["discord", "slack", "discord"])
        assert entry == {"deliver_to": ["discord", "slack"]}

    def test_unknown_transport_is_refused_and_nothing_stored(self, settings):
        with pytest.raises(ChannelSettingsError):
            settings.update("system.cron", deliver_to=["slack", "irc"])
        assert settings.get("system.cron") == {}

    @pytest.mark.parametrize("value", ["slack", [1], [None]])
    def test_non_list_route_is_refused(self, settings, value):
        with pytest.raises(ChannelSettingsError):
            settings.update("system.cron", deliver_to=value)

    def test_unknown_floor_is_refused(self, settings):
        with pytest.raises(ChannelSettingsError):
            settings.update("system.cron", deliver_to=["slack"], deliver_min_priority="loud")

    def test_floor_without_a_route_is_refused(self, settings):
        with pytest.raises(ChannelSettingsError):
            settings.update("system.cron", deliver_min_priority="all")

    def test_floor_set_on_an_existing_route(self, settings):
        settings.update("system.cron", deliver_to=["slack"])
        entry = settings.update("system.cron", deliver_min_priority="default")
        assert entry == {"deliver_to": ["slack"], "deliver_min_priority": "default"}

    def test_empty_route_clears_route_and_floor_only(self, settings):
        settings.update("system.cron", muted=True)
        settings.update("system.cron", deliver_to=["slack"], deliver_min_priority="all")
        assert settings.update("system.cron", deliver_to=[]) == {"muted": True}

    def test_protected_channel_may_be_routed(self, settings):
        entry = settings.update("system.approval", deliver_to=["telegram"])
        assert entry == {"deliver_to": ["telegram"]}

    def test_route_survives_a_reload(self, monkeypatch, tmp_path, settings):
        settings.update("system.cron", deliver_to=["webex"], deliver_min_priority="all")
        assert ChannelSettings().get("system.cron") == {
            "deliver_to": ["webex"],
            "deliver_min_priority": "all",
        }

    def test_apply_does_not_touch_the_route(self, settings):
        settings.update("system.cron", deliver_to=["slack"])
        note = settings.apply(_note())
        assert "deliver_to" not in note and note["priority"] == "critical"


class TestImportedRoutes:
    def test_valid_route_is_kept(self):
        text = '{"channel_settings": {"system.cron": {"deliver_to": ["slack"], "deliver_min_priority": "all"}}}'
        kept, dropped = parse_imported_settings(text)
        assert kept["system.cron"] == {"deliver_to": ["slack"], "deliver_min_priority": "all"}
        assert dropped == 0

    @pytest.mark.parametrize(
        ("entry", "expected"),
        [
            ({"deliver_to": ["irc"]}, None),
            ({"deliver_to": "slack"}, None),
            ({"deliver_min_priority": "all"}, None),
            ({"deliver_to": ["slack"], "deliver_min_priority": "loud"}, {"deliver_to": ["slack"]}),
        ],
    )
    def test_invalid_route_fields_are_dropped_and_counted(self, entry, expected):
        kept, dropped = parse_imported_settings(
            json.dumps({"channel_settings": {"system.cron": entry}})
        )
        assert kept.get("system.cron") == expected
        assert dropped >= 1


class TestCompositeSink:
    def test_local_runs_before_the_bridge(self):
        order: list[str] = []
        sink = composite_sink(lambda n: order.append("local"), lambda n, p: order.append("bridge"))
        sink(_note())
        assert order == ["local", "bridge"]

    def test_the_bridge_gets_the_publisher_the_local_sink_pops(self):
        seen: list[str] = []
        sink = composite_sink(
            lambda n: n.pop("publisher_session", None), lambda n, p: seen.append(p)
        )
        sink(_note(publisher_session="dashboard:slot-1"))
        sink(_note())
        assert seen == ["dashboard:slot-1", ""]

    def test_bridge_failure_never_reaches_the_producer(self):
        delivered: list[dict] = []

        def boom(_note, _publisher):
            raise RuntimeError("bridge broke")

        composite_sink(delivered.append, boom)(_note())
        assert len(delivered) == 1

    def test_local_failure_propagates_and_skips_the_bridge(self):
        bridged: list[dict] = []

        def boom(_note):
            raise RuntimeError("disk full")

        with pytest.raises(RuntimeError):
            composite_sink(boom, lambda n, p: bridged.append(n))(_note())
        assert bridged == []


class TestDispatcher:
    @pytest.mark.asyncio
    async def test_routed_note_reaches_each_sink_as_its_own_copy(self, settings):
        settings.update("system.cron", deliver_to=["slack", "discord"])
        dispatcher = BridgeDispatcher(settings=lambda: settings)
        slack, discord = _RecordingSink("slack"), _RecordingSink("discord")
        dispatcher.register_sink(slack)
        dispatcher.register_sink(discord)
        note = _note(meta={"a": [1]})
        dispatcher.schedule(note)
        await dispatcher.drain()
        assert slack.notes == [note] and discord.notes == [note]
        slack.notes[0]["meta"]["a"].append(2)
        assert note["meta"]["a"] == [1] and discord.notes[0]["meta"]["a"] == [1]

    @pytest.mark.asyncio
    async def test_scheduling_does_not_wait_for_delivery(self, settings):
        settings.update("system.cron", deliver_to=["slack"])
        dispatcher = BridgeDispatcher(settings=lambda: settings)
        sink = _RecordingSink("slack")
        dispatcher.register_sink(sink)
        dispatcher.schedule(_note())
        assert sink.notes == []  # scheduled, not awaited
        await dispatcher.drain()
        assert len(sink.notes) == 1

    @pytest.mark.parametrize(
        "note",
        [
            _note(priority="default"),  # below the critical floor
            _note(silenced=True),  # muted by the user
            _note(channel="system.heartbeat"),  # channel has no route
        ],
    )
    @pytest.mark.asyncio
    async def test_unmatched_note_is_not_delivered(self, settings, note):
        settings.update("system.cron", deliver_to=["slack"])
        dispatcher = BridgeDispatcher(settings=lambda: settings)
        sink = _RecordingSink("slack")
        dispatcher.register_sink(sink)
        dispatcher.schedule(note)
        await dispatcher.drain()
        assert sink.notes == []

    @pytest.mark.asyncio
    async def test_silenced_note_never_routes_even_on_the_all_floor(self, settings):
        settings.update("system.cron", deliver_to=["slack"], deliver_min_priority="all")
        dispatcher = BridgeDispatcher(settings=lambda: settings)
        assert dispatcher.routes(_note(priority="passive", silenced=True)) == ()
        assert dispatcher.routes(_note(priority="passive")) == ("slack",)

    @pytest.mark.asyncio
    async def test_one_failing_sink_does_not_stop_the_others(self, settings):
        settings.update("system.cron", deliver_to=["slack", "telegram"])
        dispatcher = BridgeDispatcher(settings=lambda: settings)
        dispatcher.register_sink(_RecordingSink("slack", fail=True))
        telegram = _RecordingSink("telegram")
        dispatcher.register_sink(telegram)
        dispatcher.schedule(_note())
        await dispatcher.drain()
        assert len(telegram.notes) == 1

    @pytest.mark.asyncio
    async def test_routed_transport_without_a_sink_is_skipped(self, settings):
        settings.update("system.cron", deliver_to=["wecom", "slack"])
        dispatcher = BridgeDispatcher(settings=lambda: settings)
        slack = _RecordingSink("slack")
        dispatcher.register_sink(slack)
        # The route still names wecom, but only slack can deliver.
        assert dispatcher.routes(_note()) == ("wecom", "slack")
        assert dispatcher.transports == ("slack",)
        dispatcher.schedule(_note())
        await dispatcher.drain()
        # The sinkless transport is skipped, not the whole fan-out.
        assert len(slack.notes) == 1

    def test_unknown_sink_transport_is_refused(self, settings):
        with pytest.raises(ValueError):
            BridgeDispatcher(settings=lambda: settings).register_sink(_RecordingSink("irc"))

    def test_unreadable_settings_route_nothing(self):
        def broken():
            raise OSError("gone")

        assert BridgeDispatcher(settings=broken).routes(_note()) == ()

    def test_no_running_loop_and_no_serving_loop_is_a_no_op(self, settings):
        settings.update("system.cron", deliver_to=["slack"])
        dispatcher = BridgeDispatcher(settings=lambda: settings)
        sink = _RecordingSink("slack")
        dispatcher.register_sink(sink)
        assert dispatcher.schedule(_note()) is None
        # Nothing was started on any loop, so nothing is left to run or to deliver.
        assert dispatcher._tasks == set()
        assert sink.notes == []

    @pytest.mark.asyncio
    async def test_worker_thread_queues_behind_the_serving_loop(self, settings):
        settings.update("system.cron", deliver_to=["slack"])
        dispatcher = BridgeDispatcher(settings=lambda: settings)
        sink = _RecordingSink("slack")
        dispatcher.register_sink(sink)
        loop = asyncio.get_running_loop()
        await asyncio.to_thread(dispatcher.schedule, _note(), serving_loop=loop)
        # The thread queued the start on this loop; a callback queued now runs after it.
        queued = loop.create_future()
        loop.call_soon(queued.set_result, None)
        await queued
        await dispatcher.drain()
        assert len(sink.notes) == 1


class TestGatewayWiring:
    @pytest.mark.asyncio
    async def test_push_bridges_the_note_the_user_settings_shaped(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        state.notification_channel_settings.update(
            "system.cron", priority="critical", deliver_to=["slack"]
        )
        sink = _RecordingSink("slack")
        state.notification_bridge.register_sink(sink)
        state.notification_bus.push(
            NotificationPayload(
                source="system", channel="system.cron", title="t", body="b", priority="default"
            )
        )
        assert sink.notes == []  # the producer's push never waits on a transport
        await state.notification_bridge.drain()
        assert len(sink.notes) == 1
        # Effective priority, after the user's override, decided the route.
        assert sink.notes[0]["priority"] == "critical"
        assert len(state._notification_log) == 1

    @pytest.mark.asyncio
    async def test_a_bridged_delivery_never_becomes_a_new_note(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        state.notification_channel_settings.update("system.cron", deliver_to=["slack", "discord"])
        pushes: list[Any] = []
        real_push = state.notification_bus.push
        monkeypatch.setattr(
            state.notification_bus, "push", lambda p: pushes.append(p) or real_push(p)
        )
        for transport in ("slack", "discord"):
            state.notification_bridge.register_sink(_RecordingSink(transport))
        state.notification_bus.push(
            NotificationPayload(
                source="system", channel="system.cron", title="t", body="b", priority="critical"
            )
        )
        await state.notification_bridge.drain()
        assert len(pushes) == 1
        assert len(state._notification_log) == 1


@pytest.mark.parametrize(
    "module", [bridge_mod, bridge_egress_mod, bridge_slack_mod, bridge_transport_mod]
)
def test_bridge_module_never_reaches_the_bus(module):
    """Loop invariant 1: nothing on the bridge's path publishes a notification."""
    tree = ast.parse(inspect.getsource(module))
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert not called & {"push", "notify", "push_notification"}
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
    }
    assert "notification_bus" not in names
    assert not any(
        isinstance(n, (ast.Import, ast.ImportFrom))
        and "kiro_crew.notifications.bus" in (getattr(n, "module", "") or "")
        for n in ast.walk(tree)
    )


def test_known_transports_match_the_rfc():
    assert BRIDGE_TRANSPORTS == ("slack", "discord", "telegram", "webex", "wecom")


def _make_app(state) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_put("/api/notifications/channels/settings", api_notification_channel_settings)
    return app


class TestRouteApi:
    @pytest.mark.asyncio
    async def test_put_stores_and_clears_a_route(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.put(
                "/api/notifications/channels/settings",
                json={
                    "channel": "system.cron",
                    "deliver_to": ["slack"],
                    "deliver_min_priority": "all",
                },
            )
            assert resp.status == 200
            assert (await resp.json())["settings"] == {
                "deliver_to": ["slack"],
                "deliver_min_priority": "all",
            }
            resp = await client.put(
                "/api/notifications/channels/settings",
                json={"channel": "system.cron", "deliver_to": None},
            )
            assert resp.status == 200
            assert (await resp.json())["settings"] == {}

    @pytest.mark.parametrize(
        "body",
        [
            {"deliver_to": ["irc"]},
            {"deliver_to": "slack"},
            {"deliver_to": [3]},
            {"deliver_to": ["slack"], "deliver_min_priority": "loud"},
            {"deliver_to": ["slack"], "deliver_min_priority": 1},
            {"deliver_min_priority": "all"},
        ],
    )
    @pytest.mark.asyncio
    async def test_put_refuses_a_bad_route_and_stores_nothing(self, monkeypatch, tmp_path, body):
        state = _make_state(monkeypatch, tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.put(
                "/api/notifications/channels/settings", json={"channel": "system.cron", **body}
            )
        assert resp.status == 400
        assert state.notification_channel_settings.get("system.cron") == {}
