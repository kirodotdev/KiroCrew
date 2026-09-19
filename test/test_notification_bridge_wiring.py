"""Composite egress wiring: the bridge as the bus's second sink.

``test_notification_bridge.py`` covers the dispatcher in isolation. This file
covers the seam it is wired into -- that the local sink still runs first and
unchanged, that the bridge sees the EFFECTIVE note, and that the routing rule
round-trips through the settings API.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers.messaging import (
    api_notification_agent_push,
    api_notification_channel_settings,
    api_notification_channels,
)
from kiro_crew.dashboard.state import DashboardState, _bridge_sink_for


def _make_state(monkeypatch, tmp_path) -> DashboardState:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.notifications.settings.config_dir", lambda: tmp_path)
    return DashboardState(
        sessions=MagicMock(count=0),
        crons=MagicMock(),
        lessons=MagicMock(),
        start_time=0.0,
    )


def _make_app(state: DashboardState, app_name: str = "") -> web.Application:
    app = web.Application()
    app["state"] = state
    if app_name:
        # What the token middleware sets for a verified APP token. A
        # dashboard-user token leaves it unset, which is the distinction both
        # handlers key off.
        @web.middleware
        async def _as_app(request, handler):
            request["app"] = app_name
            return await handler(request)

        app.middlewares.append(_as_app)
    app.router.add_get("/api/notifications/channels", api_notification_channels)
    app.router.add_put("/api/notifications/channels/settings", api_notification_channel_settings)
    return app


def _make_agent_app(state: DashboardState) -> web.Application:
    """The agent publish route behind the internal-secret marker it requires.

    ``internal_auth`` is what the real middleware sets on the validated
    X-Internal-Secret path, and the handler refuses without it, so a harness
    omitting it would only ever measure that refusal.
    """

    @web.middleware
    async def _internal(request, handler):
        request["internal_auth"] = True
        return await handler(request)

    app = web.Application(middlewares=[_internal])
    app["state"] = state
    app.router.add_post("/api/notifications/agent", api_notification_agent_push)
    return app


class TestCompositeEgress:
    def test_state_owns_a_bridge_wired_to_its_settings(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        state.notification_channel_settings.update("system.cron", deliver_to=["slack"])
        assert state.notification_bridge.routes(
            {"channel": "system.cron", "priority": "critical"}
        ) == ("slack",)

    def test_local_delivery_still_runs_and_the_bridge_is_offered_the_note(
        self, monkeypatch, tmp_path
    ):
        state = _make_state(monkeypatch, tmp_path)
        state._broadcast = MagicMock()
        state.notification_bridge = MagicMock()
        state._deliver_note({"channel": "system.cron", "priority": "default", "title": "t"})
        assert len(state._notification_log) == 1
        assert state._unread_count == 1
        state.notification_bridge.schedule.assert_called_once()

    def test_a_raising_bridge_cannot_break_local_delivery(self, monkeypatch, tmp_path):
        # The whole point of scheduling after the local sink: the dashboard
        # must keep its note even when the bridge leg is broken outright.
        state = _make_state(monkeypatch, tmp_path)
        state._broadcast = MagicMock()
        state.notification_bridge = MagicMock()
        state.notification_bridge.schedule.side_effect = RuntimeError("bridge broken")
        state._deliver_note({"channel": "system.cron", "priority": "critical", "title": "t"})
        assert len(state._notification_log) == 1
        assert state._notification_log[0]["title"] == "t"

    def test_a_missing_bridge_attribute_is_not_an_error(self, monkeypatch, tmp_path):
        # __new__-constructed test states (a pattern this repo uses) carry no
        # bridge; local delivery must not depend on one existing.
        state = _make_state(monkeypatch, tmp_path)
        state._broadcast = MagicMock()
        del state.notification_bridge
        state._deliver_note({"channel": "system.cron", "priority": "critical", "title": "t"})
        assert len(state._notification_log) == 1

    def test_the_bridge_sees_the_effective_priority_not_the_producers(self, monkeypatch, tmp_path):
        # deliver() applies the channel's settings in place BEFORE the bridge
        # is offered the note, so a user override decides what routes.
        state = _make_state(monkeypatch, tmp_path)
        state._broadcast = MagicMock()
        seen: list[dict] = []
        state.notification_bridge = MagicMock()
        state.notification_bridge.schedule.side_effect = lambda note: seen.append(dict(note))
        state.notification_channel_settings.update("my-app.x", priority="critical")
        state._deliver_note({"channel": "my-app.x", "priority": "default", "title": "t"})
        assert seen and seen[0]["priority"] == "critical"

    def test_a_muted_channel_reaches_the_bridge_as_passive(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        state._broadcast = MagicMock()
        seen: list[dict] = []
        state.notification_bridge = MagicMock()
        state.notification_bridge.schedule.side_effect = lambda note: seen.append(dict(note))
        state.notification_channel_settings.update("my-app.x", muted=True)
        state._deliver_note({"channel": "my-app.x", "priority": "critical", "title": "t"})
        assert seen and seen[0]["priority"] == "passive"
        assert seen[0]["silenced"] is True

    def test_an_unrouted_note_schedules_no_task(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        state._broadcast = MagicMock()
        assert (
            state.notification_bridge.schedule({"channel": "system.cron", "priority": "critical"})
            is None
        )


class TestPersistBeforePublish:
    """The bridge waits on the durable write, not just on the local sink."""

    @staticmethod
    def _armed_state(monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        state._broadcast = MagicMock()
        state.notification_bridge = MagicMock()
        return state

    @pytest.mark.asyncio
    async def test_a_durable_write_releases_the_bridge_leg(self, monkeypatch, tmp_path):
        state = self._armed_state(monkeypatch, tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.state._persist_notification", lambda _note: True)
        state._deliver_note({"channel": "system.cron", "priority": "critical", "title": "t"})
        assert await state.last_notification_persist is True
        await asyncio.sleep(0)
        state.notification_bridge.schedule.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_failed_write_withholds_the_bridge_leg(self, monkeypatch, tmp_path):
        # The push handler turns a failed persist into a 500 and the producer
        # retries, which re-delivers this note. Egressing now would make that
        # retry a duplicate DM -- and unlike a duplicate dashboard row, a DM
        # cannot be taken back.
        state = self._armed_state(monkeypatch, tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.state._persist_notification", lambda _note: False)
        state._deliver_note({"channel": "system.cron", "priority": "critical", "title": "t"})
        assert await state.last_notification_persist is False
        await asyncio.sleep(0)
        state.notification_bridge.schedule.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_raising_persist_withholds_the_bridge_leg(self, monkeypatch, tmp_path):
        def boom(_note):
            raise OSError("disk full")

        state = self._armed_state(monkeypatch, tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.state._persist_notification", boom)
        state._deliver_note({"channel": "system.cron", "priority": "critical", "title": "t"})
        with pytest.raises(OSError):
            await state.last_notification_persist
        await asyncio.sleep(0)
        state.notification_bridge.schedule.assert_not_called()

    @pytest.mark.asyncio
    async def test_local_delivery_still_completes_when_the_write_fails(self, monkeypatch, tmp_path):
        # Withholding the bridge must not withhold the dashboard: the user can
        # still read the note, which is what makes the trade acceptable.
        state = self._armed_state(monkeypatch, tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.state._persist_notification", lambda _note: False)
        state._deliver_note({"channel": "system.cron", "priority": "critical", "title": "t"})
        assert await state.last_notification_persist is False
        assert len(state._notification_log) == 1
        assert state._notification_log[0]["title"] == "t"

    @pytest.mark.asyncio
    async def test_the_bridge_reads_a_snapshot_taken_before_the_await(self, monkeypatch, tmp_path):
        # The bridge now runs after an await boundary, and acknowledgement and
        # the sweep mutate the stored row in place.
        state = self._armed_state(monkeypatch, tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.state._persist_notification", lambda _note: True)
        note = {"channel": "system.cron", "priority": "critical", "title": "original"}
        state._deliver_note(note)
        note["title"] = "mutated while the write was in flight"
        assert await state.last_notification_persist is True
        await asyncio.sleep(0)
        handed = state.notification_bridge.schedule.call_args.args[0]
        assert handed["title"] == "original"

    def test_a_synchronous_delivery_still_offers_the_note(self, monkeypatch, tmp_path):
        # No running loop: the coordinator persists inline. A successful inline
        # write IS durable, so the leg is released.
        state = self._armed_state(monkeypatch, tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.state._persist_notification", lambda _note: True)
        state._deliver_note({"channel": "system.cron", "priority": "critical", "title": "t"})
        assert state.last_notification_persist is None
        state.notification_bridge.schedule.assert_called_once()

    def test_a_failed_inline_write_withholds_the_bridge_leg(self, monkeypatch, tmp_path):
        # The path with no future AND no 500: an off-loop producer whose durable
        # write failed. Nothing will retry it, so the inline boolean is the only
        # durability answer that exists and a falsy one must withhold the leg.
        state = self._armed_state(monkeypatch, tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.state._persist_notification", lambda _note: False)
        state._deliver_note({"channel": "system.cron", "priority": "critical", "title": "t"})
        assert state.last_notification_persist is None
        state.notification_bridge.schedule.assert_not_called()
        # The dashboard still has it, which is what makes withholding acceptable.
        assert len(state._notification_log) == 1

    def test_concurrent_inline_deliveries_do_not_swap_verdicts(self, monkeypatch, tmp_path):
        # The verdict travels WITH its delivery rather than through a field on
        # the state: two off-loop producers run concurrently, so a shared slot
        # could hand one note the other's answer -- bridging a failed write or
        # withholding a good one. Interleave two deliveries whose writes differ
        # and check each got its own answer.
        import threading

        state = self._armed_state(monkeypatch, tmp_path)
        scheduled: list[str] = []
        state.notification_bridge.schedule.side_effect = lambda note: scheduled.append(
            note["title"]
        )
        both_inside = threading.Barrier(2, timeout=5)

        def persist(note):
            # Hold both writers inside the persist at once, which is what makes
            # a shared verdict slot observably wrong.
            both_inside.wait()
            return note["title"] == "good"

        monkeypatch.setattr("kiro_crew.dashboard.state._persist_notification", persist)

        def deliver(title: str) -> None:
            state._deliver_note({"channel": "system.cron", "priority": "critical", "title": title})

        threads = [threading.Thread(target=deliver, args=(t,)) for t in ("good", "bad")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert scheduled == ["good"]


class TestSinkResolution:
    def test_slack_resolves_through_its_dedicated_client(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        state.slack_client = MagicMock()
        state.owner_id = "U1"
        state.slack_socket_connected = True
        sink = _bridge_sink_for(state, "slack")
        assert sink is not None
        assert sink.transport_id == "slack"

    def test_slack_is_unresolved_while_disconnected(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        state.slack_client = MagicMock()
        state.owner_id = "U1"
        state.slack_socket_connected = False
        assert _bridge_sink_for(state, "slack") is None

    def test_phase_b2_transports_are_not_wired_yet(self, monkeypatch, tmp_path):
        # Explicit rather than incidental: a route to one of these is armed,
        # validated and persisted, and the bridge audits it as a skip. B2 adds
        # the sinks; nothing about the rule has to change then.
        state = _make_state(monkeypatch, tmp_path)
        for transport in ("discord", "telegram", "webex", "wecom"):
            assert _bridge_sink_for(state, transport) is None


class TestDeliveryRoutingIsOwnerOnly:
    """The two routing fields decide whether the OWNER's notifications leave the
    machine as chat DMs, so an installed app must not set or read them.

    The transport layer's exclusion is declarative and defeatable, which is why
    the handlers check rather than trusting it: ``app_token_path_allowed`` grants
    only ``/api/notifications/push`` and its comment says app tokens must not
    reach ``/api/notifications``, but its last clause still honours the app's own
    ``permissions.api`` and ``_api_pattern_matches`` treats a bare
    ``/api/notifications`` prefix as covering every child path.
    """

    @pytest.mark.asyncio
    async def test_an_app_token_cannot_set_the_routing_fields(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        async with TestClient(TestServer(_make_app(state, app_name="rogue"))) as client:
            resp = await client.put(
                "/api/notifications/channels/settings",
                json={"channel": "system.cron", "deliver_to": ["slack"]},
            )
            status, payload = resp.status, await resp.json()
        assert status == 403
        assert payload["code"] == "delivery_routing_owner_only"
        # And nothing was written: a refusal that still persisted would be worse
        # than no check, because the caller reads 403 and the route is armed.
        assert state.notification_channel_settings.all_settings().get("system.cron", {}) == {}

    @pytest.mark.asyncio
    async def test_an_app_token_cannot_set_the_floor_either(self, monkeypatch, tmp_path):
        # Both keys, not just the obvious one: a floor of `all` on an armed
        # channel turns every passive note into a DM.
        state = _make_state(monkeypatch, tmp_path)
        async with TestClient(TestServer(_make_app(state, app_name="rogue"))) as client:
            resp = await client.put(
                "/api/notifications/channels/settings",
                json={"channel": "system.cron", "deliver_min_priority": "all"},
            )
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_an_app_token_may_still_mute(self, monkeypatch, tmp_path):
        # The pre-existing contract. Guarding the FIELDS rather than the route is
        # what keeps this working for an app already using it, and it is also
        # what stops the guard from passing by refusing everything.
        state = _make_state(monkeypatch, tmp_path)
        async with TestClient(TestServer(_make_app(state, app_name="tidy"))) as client:
            resp = await client.put(
                "/api/notifications/channels/settings",
                json={"channel": "system.cron", "muted": True},
            )
        assert resp.status == 200
        assert state.notification_channel_settings.all_settings()["system.cron"]["muted"] is True

    @pytest.mark.asyncio
    async def test_the_owner_can_set_the_routing_fields(self, monkeypatch, tmp_path):
        # No app identity means a dashboard user, which is whose call this is.
        state = _make_state(monkeypatch, tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.put(
                "/api/notifications/channels/settings",
                json={"channel": "system.cron", "deliver_to": ["slack"]},
            )
        assert resp.status == 200
        assert state.notification_bridge.routes(
            {"channel": "system.cron", "priority": "critical"}
        ) == ("slack",)

    @pytest.mark.asyncio
    async def test_an_app_token_does_not_read_the_owners_routing(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        state.notification_channel_settings.update("system.cron", muted=True, deliver_to=["slack"])
        async with TestClient(TestServer(_make_app(state, app_name="nosy"))) as client:
            body = await (await client.get("/api/notifications/channels")).json()
        row = next(r for r in body["channels"] if r["channel"] == "system.cron")
        assert "deliver_to" not in row["settings"]
        assert "deliver_min_priority" not in row["settings"]
        # The pre-existing field survives the redaction.
        assert row["settings"]["muted"] is True

    @pytest.mark.asyncio
    async def test_the_owner_does_read_the_routing(self, monkeypatch, tmp_path):
        # Paired with the test above so the redaction cannot pass by emptying
        # the settings for everyone.
        state = _make_state(monkeypatch, tmp_path)
        state.notification_channel_settings.update("system.cron", deliver_to=["slack"])
        async with TestClient(TestServer(_make_app(state))) as client:
            body = await (await client.get("/api/notifications/channels")).json()
        row = next(r for r in body["channels"] if r["channel"] == "system.cron")
        assert row["settings"]["deliver_to"] == ["slack"]

    def test_the_guarded_key_set_is_declared_once(self):
        # Both handlers derive their check from this set, so a third routing key
        # added later is refused and withheld without touching either handler.
        from kiro_crew.notifications.settings import DELIVERY_SETTING_KEYS

        assert DELIVERY_SETTING_KEYS == frozenset({"deliver_to", "deliver_min_priority"})


class TestRoutingApi:
    @pytest.mark.asyncio
    async def test_get_lists_the_routable_transports_and_their_reachability(
        self, monkeypatch, tmp_path
    ):
        state = _make_state(monkeypatch, tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            body = await (await client.get("/api/notifications/channels")).json()
        by_id = {row["transport"]: row for row in body["bridge_transports"]}
        assert set(by_id) == {"slack", "discord", "telegram", "webex", "wecom"}
        assert by_id["slack"]["connected"] is False
        assert by_id["slack"]["bridgeable"] is True

    @pytest.mark.asyncio
    async def test_get_separates_connected_from_bridgeable(self, monkeypatch, tmp_path):
        # Two facts, not one. A transport can be up and still have no bridge
        # sink, and a picker told only "connected" would offer a row whose every
        # delivery is an audited skip.
        state = _make_state(monkeypatch, tmp_path)
        monkeypatch.setattr(
            type(state),
            "channel_status",
            lambda _self: {t: {"connected": True, "error": ""} for t in ("slack", "telegram")},
            raising=False,
        )
        async with TestClient(TestServer(_make_app(state))) as client:
            body = await (await client.get("/api/notifications/channels")).json()
        by_id = {row["transport"]: row for row in body["bridge_transports"]}
        assert set(by_id) == {"slack", "discord", "telegram", "webex", "wecom"}
        assert by_id["slack"] == {"transport": "slack", "connected": True, "bridgeable": True}
        # Connected but not deliverable until phase B2 ships its sink.
        assert by_id["telegram"] == {
            "transport": "telegram",
            "connected": True,
            "bridgeable": False,
        }
        assert by_id["webex"]["connected"] is False
        assert by_id["webex"]["bridgeable"] is False

    @pytest.mark.asyncio
    async def test_bridgeable_matches_what_the_resolver_can_actually_build(
        self, monkeypatch, tmp_path
    ):
        # One declaration behind both, so the payload and the resolver cannot
        # drift apart as B2 lands transports one at a time.
        state = _make_state(monkeypatch, tmp_path)
        state.slack_client = MagicMock()
        state.owner_id = "U1"
        state.slack_socket_connected = True
        async with TestClient(TestServer(_make_app(state))) as client:
            body = await (await client.get("/api/notifications/channels")).json()
        for row in body["bridge_transports"]:
            built = _bridge_sink_for(state, row["transport"])
            if row["bridgeable"]:
                assert built is not None or row["transport"] != "slack"
            else:
                assert built is None

    @pytest.mark.asyncio
    async def test_put_arms_a_route_with_the_default_floor(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        state.broadcast_ws = MagicMock()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.put(
                "/api/notifications/channels/settings",
                json={"channel": "system.approval", "deliver_to": ["slack"]},
            )
            body = await resp.json()
        assert resp.status == 200
        assert body["settings"]["deliver_to"] == ["slack"]
        assert body["settings"]["deliver_min_priority"] == "critical"

    @pytest.mark.asyncio
    async def test_put_accepts_an_explicit_floor(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        state.broadcast_ws = MagicMock()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.put(
                "/api/notifications/channels/settings",
                json={
                    "channel": "system.cron",
                    "deliver_to": ["slack", "telegram"],
                    "deliver_min_priority": "all",
                },
            )
            body = await resp.json()
        assert resp.status == 200
        assert body["settings"]["deliver_to"] == ["slack", "telegram"]
        assert body["settings"]["deliver_min_priority"] == "all"

    @pytest.mark.asyncio
    async def test_put_empty_list_disarms_the_route(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        state.broadcast_ws = MagicMock()
        state.notification_channel_settings.update("system.cron", deliver_to=["slack"])
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.put(
                "/api/notifications/channels/settings",
                json={"channel": "system.cron", "deliver_to": []},
            )
            body = await resp.json()
        assert resp.status == 200
        assert "deliver_to" not in body["settings"]
        assert "deliver_min_priority" not in body["settings"]

    @pytest.mark.asyncio
    async def test_put_null_clears_the_route(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        state.broadcast_ws = MagicMock()
        state.notification_channel_settings.update("system.cron", deliver_to=["slack"])
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.put(
                "/api/notifications/channels/settings",
                json={"channel": "system.cron", "deliver_to": None},
            )
            body = await resp.json()
        assert resp.status == 200
        assert "deliver_to" not in body["settings"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "payload",
        [
            {"channel": "a.b", "deliver_to": "slack"},
            {"channel": "a.b", "deliver_to": ["pigeon"]},
            {"channel": "a.b", "deliver_to": [7]},
            {"channel": "a.b", "deliver_to": ["slack"], "deliver_min_priority": "whenever"},
            {"channel": "a.b", "deliver_to": ["slack"], "deliver_min_priority": 3},
        ],
    )
    async def test_put_rejects_an_unusable_rule(self, monkeypatch, tmp_path, payload):
        state = _make_state(monkeypatch, tmp_path)
        state.broadcast_ws = MagicMock()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.put("/api/notifications/channels/settings", json=payload)
        assert resp.status == 400
        assert state.notification_channel_settings.get("a.b") == {}

    @pytest.mark.asyncio
    async def test_a_route_armed_over_http_is_what_the_bridge_reads(self, monkeypatch, tmp_path):
        # End to end across the seam: the rule the API wrote is the rule the
        # dispatcher routes on, with no second copy in between.
        state = _make_state(monkeypatch, tmp_path)
        state.broadcast_ws = MagicMock()
        async with TestClient(TestServer(_make_app(state))) as client:
            await client.put(
                "/api/notifications/channels/settings",
                json={"channel": "system.approval", "deliver_to": ["slack"]},
            )
        assert state.notification_bridge.routes(
            {"channel": "system.approval", "priority": "critical"}
        ) == ("slack",)
        assert (
            state.notification_bridge.routes({"channel": "system.approval", "priority": "default"})
            == ()
        )


class TestEndToEndDelivery:
    @pytest.mark.asyncio
    async def test_a_critical_note_on_a_routed_channel_reaches_slack(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        state._broadcast = MagicMock()
        client = MagicMock()
        client.open_dm = AsyncMock(return_value="D1")
        client.post_message = AsyncMock(return_value="ts-1")
        state.slack_client = client
        state.owner_id = "U1"
        state.slack_socket_connected = True
        state.notification_channel_settings.update("system.approval", deliver_to=["slack"])
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.vet_and_audit",
            lambda *a, **k: MagicMock(permitted=True, rule="", layer="", reason=""),
        )
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: MagicMock())
        monkeypatch.setattr("kiro_crew.dashboard.state._persist_notification", lambda _note: True)
        state._deliver_note(
            {
                "channel": "system.approval",
                "priority": "critical",
                "title": "Approval needed",
                "body": "agent wants to run rm",
            }
        )
        # The whole chain, in order: the note is in memory, its durable write
        # lands, and only then does the bridge leg run.
        assert len(state._notification_log) == 1
        assert await state.last_notification_persist is True
        await asyncio.sleep(0)
        await state.notification_bridge.drain(timeout=5)
        client.open_dm.assert_awaited_once_with("U1")
        posted = client.post_message.await_args.args[1]
        assert "Approval needed" in posted

    @pytest.mark.asyncio
    async def test_an_off_loop_note_with_a_failed_write_never_reaches_slack(
        self, monkeypatch, tmp_path
    ):
        # The exact chain the review named: an off-loop producer (the shape
        # `asyncio.to_thread(state.notify, ...)` produces) persists inline, the
        # write fails, and there is no future and no 500 to make anyone retry.
        # The gateway loop IS reachable, so without the inline verdict this
        # would deliver a note absent from durable history.
        state = _make_state(monkeypatch, tmp_path)
        state._broadcast = MagicMock()
        client = MagicMock()
        client.open_dm = AsyncMock(return_value="D1")
        client.post_message = AsyncMock(return_value="ts-1")
        state.slack_client = client
        state.owner_id = "U1"
        state.slack_socket_connected = True
        state.bind_serving_loop(asyncio.get_running_loop())
        state.notification_channel_settings.update("system.agent", deliver_to=["slack"])
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.vet_and_audit",
            lambda *a, **k: MagicMock(permitted=True, rule="", layer="", reason=""),
        )
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: MagicMock())
        monkeypatch.setattr("kiro_crew.dashboard.state._persist_notification", lambda _note: False)

        await asyncio.to_thread(
            state._deliver_note,
            {"channel": "system.agent", "priority": "critical", "title": "Code review ready"},
        )
        for _ in range(6):
            await asyncio.sleep(0)
        await state.notification_bridge.drain(timeout=5)
        client.open_dm.assert_not_awaited()
        client.post_message.assert_not_awaited()
        assert len(state._notification_log) == 1

    @pytest.mark.asyncio
    async def test_an_off_loop_note_with_a_good_write_does_reach_slack(self, monkeypatch, tmp_path):
        # The other half, so the guard above is not just refusing everything:
        # the same off-loop producer with a durable write DOES deliver.
        state = _make_state(monkeypatch, tmp_path)
        state._broadcast = MagicMock()
        client = MagicMock()
        client.open_dm = AsyncMock(return_value="D1")
        client.post_message = AsyncMock(return_value="ts-1")
        state.slack_client = client
        state.owner_id = "U1"
        state.slack_socket_connected = True
        state.bind_serving_loop(asyncio.get_running_loop())
        state.notification_channel_settings.update("system.agent", deliver_to=["slack"])
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.vet_and_audit",
            lambda *a, **k: MagicMock(permitted=True, rule="", layer="", reason=""),
        )
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: MagicMock())
        monkeypatch.setattr("kiro_crew.dashboard.state._persist_notification", lambda _note: True)

        await asyncio.to_thread(
            state._deliver_note,
            {"channel": "system.agent", "priority": "critical", "title": "Code review ready"},
        )
        for _ in range(8):
            await asyncio.sleep(0)
            if client.post_message.await_count:
                break
        await state.notification_bridge.drain(timeout=5)
        client.open_dm.assert_awaited_once_with("U1")
        assert "Code review ready" in client.post_message.await_args.args[1]


class TestAgentNotesNameTheirProducingSession:
    """An agent note must carry the session that produced it.

    Without it the bridge has no session subject for ANY agent note: ``source``
    is the server-fixed ``"system"`` and nothing else on the note names a
    producer, so only the host surface is vetted. That is the gap: an agent whose
    governance profile denies ``channels/slack`` is refused by ``send_message``
    on that transport, then egresses to the same Slack DM through
    ``send_notification``. One producer, one transport, one policy, two answers.

    The bridge could already vet a named session before this -- these tests cover
    the half that was missing, which is this route naming one.
    """

    @pytest.mark.asyncio
    async def test_the_note_carries_the_calling_session(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        # The handler refuses a ``dashboard:`` key whose slot is gone, which is a
        # separate pre-existing guard. Registering the slot is what lets this test
        # measure the attribution rather than that refusal.
        state._slots["chat-7"] = object()
        async with TestClient(TestServer(_make_agent_app(state))) as client:
            resp = await client.post(
                "/api/notifications/agent",
                json={"title": "review ready"},
                headers={"X-Session-Key": "dashboard:chat-7"},
            )
            assert resp.status == 200
            note = (await resp.json())["note"]
        assert note["session_key"] == "dashboard:chat-7"

    @pytest.mark.asyncio
    async def test_a_slotless_producer_is_named_too(self, monkeypatch, tmp_path):
        # A cron is a real ``send_notification`` producer that never had a slot,
        # so the missing-slot guard does not apply to it and its own profile is
        # the only per-producer policy there is. Its key must still reach the note.
        state = _make_state(monkeypatch, tmp_path)
        async with TestClient(TestServer(_make_agent_app(state))) as client:
            resp = await client.post(
                "/api/notifications/agent",
                json={"title": "nightly done"},
                headers={"X-Session-Key": "cron:job-7"},
            )
            assert resp.status == 200
            note = (await resp.json())["note"]
        assert note["session_key"] == "cron:job-7"

    @pytest.mark.asyncio
    async def test_the_request_body_cannot_choose_the_session(self, monkeypatch, tmp_path):
        # The whole value of the field is that the producer did not pick it. The
        # route never passes the body's ``meta`` into the payload, so a body key
        # has no door here -- pinned because opening one later would turn a
        # governance subject into a caller-chosen one.
        state = _make_state(monkeypatch, tmp_path)
        async with TestClient(TestServer(_make_agent_app(state))) as client:
            resp = await client.post(
                "/api/notifications/agent",
                json={
                    "title": "t",
                    "session_key": "cron:permissive",
                    "meta": {"session_key": "cron:permissive", "caller": "cron:permissive"},
                },
                headers={"X-Session-Key": "cron:job-7"},
            )
            assert resp.status == 200
            note = (await resp.json())["note"]
        assert note["session_key"] == "cron:job-7"
        assert note.get("caller") in (None, "")

    @pytest.mark.asyncio
    async def test_no_session_header_leaves_the_field_absent(self, monkeypatch, tmp_path):
        # A caller that genuinely has no session (a boot-time note) must not get
        # an invented subject: a fabricated key would infer some surface and have
        # an unrelated profile answer for it. Absent means "host only", which is
        # the pre-existing behaviour and still correct for a host-produced note.
        state = _make_state(monkeypatch, tmp_path)
        async with TestClient(TestServer(_make_agent_app(state))) as client:
            resp = await client.post("/api/notifications/agent", json={"title": "t"})
            assert resp.status == 200
            note = (await resp.json())["note"]
        assert "session_key" not in note
