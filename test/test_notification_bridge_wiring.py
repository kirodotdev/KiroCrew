"""Composite egress wiring: the bridge as the bus's second sink.

``test_notification_bridge.py`` covers the dispatcher in isolation. This file
covers the seam it is wired into -- that the local sink still runs first and
unchanged, that the bridge sees the EFFECTIVE note, and that the routing rule
round-trips through the settings API.
"""

from __future__ import annotations

import asyncio
import json
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
from kiro_crew.notifications.settings import ChannelSettingsAuthError, ChannelSettingsError

#: The dashboard owner, and one other subject that token auth admits just as
#: readily. ``!dashboard`` hands an allow-listed messaging user an ordinary
#: dashboard session, so the second is a real principal rather than a fabricated
#: one, and the routing fields are exactly what separates them.
_OWNER_SUBJECT = "U1"
_NON_OWNER_SUBJECT = "U2"


def _make_state(monkeypatch, tmp_path) -> DashboardState:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.notifications.settings.config_dir", lambda: tmp_path)
    state = DashboardState(
        sessions=MagicMock(count=0),
        crons=MagicMock(),
        lessons=MagicMock(),
        start_time=0.0,
    )
    state.owner_id = _OWNER_SUBJECT
    return state


def _make_app(
    state: DashboardState, app_name: str = "", user: str | None = None
) -> web.Application:
    """The two notification routes behind the principal the token middleware sets.

    Three principals reach these handlers, and the owner gate reads BOTH claims to
    tell them apart, so the double sets both. An app token carries its name in
    ``app``. A dashboard session carries ``app == ""`` plus a ``user`` subject, and
    whether that subject is the owner is the whole question the routing fields
    turn on -- so ``user`` defaults to the state's owner, and a caller passes a
    different subject to be an ordinary non-owner session instead.
    """
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def _principal(request, handler):
        request["app"] = app_name
        if not app_name:
            request["user"] = (
                user if user is not None else str(getattr(state, "owner_id", "") or "")
            )
        return await handler(request)

    app.middlewares.append(_principal)
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
    async def test_a_passive_priority_would_suppress_an_armed_route(self, monkeypatch, tmp_path):
        # The mechanism the two refusals below exist for, asserted on its own so
        # they cannot pass as fences over something that was never reachable.
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
        # Same armed rule, same channel: only the effective priority differs.
        assert (
            state.notification_bridge.routes({"channel": "system.cron", "priority": "passive"})
            == ()
        )

    @pytest.mark.asyncio
    async def test_an_app_token_cannot_mute_a_channel_that_routes(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        async with TestClient(TestServer(_make_app(state))) as owner:
            assert (
                await owner.put(
                    "/api/notifications/channels/settings",
                    json={"channel": "system.cron", "deliver_to": ["slack"]},
                )
            ).status == 200
        async with TestClient(TestServer(_make_app(state, app_name="rogue"))) as client:
            resp = await client.put(
                "/api/notifications/channels/settings",
                json={"channel": "system.cron", "muted": True},
            )
            status, payload = resp.status, await resp.json()
        assert status == 403
        assert payload["code"] == "delivery_routing_owner_only"
        # And nothing was written. A refusal that still persisted would be worse
        # than no check: the caller reads 403 and the owner's DMs are silent.
        stored = state.notification_channel_settings.all_settings()["system.cron"]
        assert "muted" not in stored
        assert state.notification_bridge.routes(
            {"channel": "system.cron", "priority": "critical"}
        ) == ("slack",)

    @pytest.mark.asyncio
    async def test_an_app_token_cannot_down_rank_a_channel_that_routes(self, monkeypatch, tmp_path):
        # The sharper half: `priority` never names a routing field, so the
        # routing-field refusal above does not see this request at all.
        state = _make_state(monkeypatch, tmp_path)
        async with TestClient(TestServer(_make_app(state))) as owner:
            assert (
                await owner.put(
                    "/api/notifications/channels/settings",
                    json={"channel": "system.cron", "deliver_to": ["slack"]},
                )
            ).status == 200
        async with TestClient(TestServer(_make_app(state, app_name="rogue"))) as client:
            resp = await client.put(
                "/api/notifications/channels/settings",
                json={"channel": "system.cron", "priority": "passive"},
            )
        assert resp.status == 403
        assert "priority" not in state.notification_channel_settings.all_settings()["system.cron"]

    def test_the_auth_error_fails_closed_for_a_caller_that_misses_it(self) -> None:
        """A caller handling only the base class must still refuse the write.

        The refusal is raised inside the writer lock, before the entry is persisted
        or committed to memory, so the subclassing decides only how precisely the
        refusal is reported -- never whether the write happens.
        """
        assert issubclass(ChannelSettingsAuthError, ChannelSettingsError)

    @pytest.mark.asyncio
    async def test_arming_a_route_discards_a_prior_apps_mute(self, monkeypatch, tmp_path):
        """Arming is when display state becomes delivery authority.

        The fence refuses the pair only while a route is ALREADY armed. Writing it
        first and letting the owner arm afterwards reaches the same end without ever
        tripping the fence: `apply()` forces a muted armed channel to `passive`, and
        the retained value then suppresses every DM on the route.
        """
        state = _make_state(monkeypatch, tmp_path)
        store = state.notification_channel_settings
        # Legitimate under the preserved contract: the channel is unarmed.
        async with TestClient(TestServer(_make_app(state, app_name="tidy"))) as app_client:
            assert (
                await app_client.put(
                    "/api/notifications/channels/settings",
                    json={"channel": "system.cron", "muted": True},
                )
            ).status == 200
        assert store.get("system.cron")["muted"] is True
        # The owner arms afterwards and says nothing about mute.
        async with TestClient(TestServer(_make_app(state))) as owner:
            assert (
                await owner.put(
                    "/api/notifications/channels/settings",
                    json={"channel": "system.cron", "deliver_to": ["slack"]},
                )
            ).status == 200
        stored = store.get("system.cron")
        assert "muted" not in stored
        assert stored["deliver_to"] == ["slack"]
        # The end the finding cares about: the route actually delivers.
        assert state.notification_bridge.routes(
            {"channel": "system.cron", "priority": "critical"}
        ) == ("slack",)

    @pytest.mark.asyncio
    async def test_an_owner_priority_edit_does_not_launder_an_apps_mute(
        self, monkeypatch, tmp_path
    ):
        """Provenance is per FIELD, and this is the sequence that proves it.

        With one mark for the pair, the owner's priority-only edit clears it while the
        app's `muted` is still stored; the later arm then sees no mark, keeps the mute,
        and `apply()` forces the armed channel to `passive` -- suppressing the DM the
        owner just armed. Every step here is an ordinary operation.
        """
        state = _make_state(monkeypatch, tmp_path)
        store = state.notification_channel_settings
        async with TestClient(TestServer(_make_app(state, app_name="tidy"))) as app_client:
            assert (
                await app_client.put(
                    "/api/notifications/channels/settings",
                    json={"channel": "system.cron", "muted": True},
                )
            ).status == 200
        async with TestClient(TestServer(_make_app(state))) as owner:
            # The owner touches ONLY priority. The app's mute is untouched and still
            # carries no authority of its own.
            assert (
                await owner.put(
                    "/api/notifications/channels/settings",
                    json={"channel": "system.cron", "priority": "critical"},
                )
            ).status == 200
            assert (
                await owner.put(
                    "/api/notifications/channels/settings",
                    json={"channel": "system.cron", "deliver_to": ["slack"]},
                )
            ).status == 200
        stored = store.get("system.cron")
        assert "muted" not in stored
        assert stored["priority"] == "critical"
        assert state.notification_bridge.routes(
            {"channel": "system.cron", "priority": "critical"}
        ) == ("slack",)

    @pytest.mark.asyncio
    async def test_arming_keeps_the_owners_own_prior_mute(self, monkeypatch, tmp_path):
        """The other half of the discard, and the reason it needs provenance.

        The owner's mute is theirs to keep: arming a route must not silently clear a
        preference they set deliberately. Only an override written by a caller with no
        routing authority is dropped, so the marker is what tells the two apart rather
        than the value itself, which is identical in both cases.
        """
        state = _make_state(monkeypatch, tmp_path)
        store = state.notification_channel_settings
        async with TestClient(TestServer(_make_app(state))) as owner:
            assert (
                await owner.put(
                    "/api/notifications/channels/settings",
                    json={"channel": "system.cron", "muted": True},
                )
            ).status == 200
            assert (
                await owner.put(
                    "/api/notifications/channels/settings",
                    json={"channel": "system.cron", "deliver_to": ["slack"]},
                )
            ).status == 200
        stored = store.get("system.cron")
        assert stored["muted"] is True
        assert stored["deliver_to"] == ["slack"]

    @pytest.mark.asyncio
    async def test_arming_drops_a_stored_mute_whose_provenance_is_unrecorded(
        self, monkeypatch, tmp_path
    ):
        """A row with no provenance stamp cannot have its mute credited to the owner.

        Absence of the app marker is not evidence of owner authorship: an owner write
        clears that marker too, so the two are indistinguishable by it. Only the stamp
        separates them, and a row that carries a display field without one is held to
        carry no routing authority -- otherwise a mute stored by a caller that never had
        that authority survives arming and silences the DM the owner just armed.

        The owner keeps both halves of the recovery the fence promises: the value stays
        visible to them, and naming it reaffirms it.
        """
        state = _make_state(monkeypatch, tmp_path)
        store = state.notification_channel_settings
        # A stored row holding a display field and neither bookkeeping key -- the shape
        # no write through `update()` can produce.
        store._settings = {"system.cron": {"muted": True}}
        assert (
            store.get("system.cron")["muted"] is True
        ), "the owner cannot see the value they are being asked to reaffirm"

        async with TestClient(TestServer(_make_app(state))) as owner:
            assert (
                await owner.put(
                    "/api/notifications/channels/settings",
                    json={"channel": "system.cron", "deliver_to": ["slack"]},
                )
            ).status == 200
        stored = store.get("system.cron")
        assert stored["deliver_to"] == ["slack"]
        assert (
            "muted" not in stored
        ), f"an unattributable mute kept authority over an armed route: {stored}"

        # Reaffirmed: the owner names the field, which is what makes it theirs, and it
        # then survives arming exactly as their own settled preference does.
        store._settings = {"system.cron": {"muted": True}}
        async with TestClient(TestServer(_make_app(state))) as owner:
            assert (
                await owner.put(
                    "/api/notifications/channels/settings",
                    json={"channel": "system.cron", "muted": True},
                )
            ).status == 200
            assert (
                await owner.put(
                    "/api/notifications/channels/settings",
                    json={"channel": "system.cron", "deliver_to": ["slack"]},
                )
            ).status == 200
        reaffirmed = store.get("system.cron")
        assert (
            reaffirmed["muted"] is True
        ), f"the owner reaffirmed the mute and arming dropped it anyway: {reaffirmed}"
        assert reaffirmed["deliver_to"] == ["slack"]

    @pytest.mark.asyncio
    async def test_a_non_owner_dashboard_session_cannot_set_the_routing_fields(
        self, monkeypatch, tmp_path
    ):
        """An app is not the only principal without routing authority.

        ``!dashboard`` hands an allow-listed messaging user an ordinary dashboard
        session: ``app`` is the empty string and the subject is not the owner's. A
        condition on the ``app`` claim alone reads as a fence and lets that session
        arm the owner's DMs, so ownership is what the field asks for.
        """
        state = _make_state(monkeypatch, tmp_path)
        app = _make_app(state, user=_NON_OWNER_SUBJECT)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put(
                "/api/notifications/channels/settings",
                json={"channel": "system.cron", "deliver_to": ["slack"]},
            )
            status, payload = resp.status, await resp.json()
        assert status == 403, "a non-owner dashboard session armed the owner's DMs"
        assert payload["code"] == "delivery_routing_owner_only"
        # Nothing written: a refusal that still persisted would be worse than no
        # check, because the caller reads 403 and the route is armed anyway.
        assert state.notification_channel_settings.all_settings().get("system.cron", {}) == {}
        assert (
            state.notification_bridge.routes({"channel": "system.cron", "priority": "critical"})
            == ()
        )

    @pytest.mark.asyncio
    async def test_a_non_owner_dashboard_session_cannot_set_the_floor_either(
        self, monkeypatch, tmp_path
    ):
        # Both keys, as for an app: a floor of `all` on an armed channel turns
        # every passive note into a DM.
        state = _make_state(monkeypatch, tmp_path)
        app = _make_app(state, user=_NON_OWNER_SUBJECT)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put(
                "/api/notifications/channels/settings",
                json={"channel": "system.cron", "deliver_min_priority": "all"},
            )
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_a_non_owner_dashboard_session_is_not_shown_the_routing_fields(
        self, monkeypatch, tmp_path
    ):
        """The read carries the same authority question as the write.

        Both directions are asserted together because a gate on one alone leaves
        the owner's routing choice legible to a caller that may not change it, and
        the owner's own read is what proves the withholding is a gate rather than
        the fields simply being absent.
        """
        state = _make_state(monkeypatch, tmp_path)
        async with TestClient(TestServer(_make_app(state))) as owner:
            assert (
                await owner.put(
                    "/api/notifications/channels/settings",
                    json={"channel": "system.cron", "deliver_to": ["slack"], "muted": True},
                )
            ).status == 200
            owner_rows = (await (await owner.get("/api/notifications/channels")).json())["channels"]
        owner_view = [r for r in owner_rows if r["channel"] == "system.cron"][0]["settings"]
        assert owner_view["deliver_to"] == [
            "slack"
        ], f"the owner cannot see their own route: {owner_view}"

        app = _make_app(state, user=_NON_OWNER_SUBJECT)
        async with TestClient(TestServer(app)) as other:
            other_rows = (await (await other.get("/api/notifications/channels")).json())["channels"]
        other_view = [r for r in other_rows if r["channel"] == "system.cron"][0]["settings"]
        assert (
            "deliver_to" not in other_view and "deliver_min_priority" not in other_view
        ), f"a non-owner session read the owner's routing choice: {other_view}"
        # The pre-existing display state is still theirs to read, which is what
        # makes this a withholding of the two fields rather than a refused route.
        assert other_view.get("muted") is True

    @pytest.mark.asyncio
    async def test_a_non_owner_dashboard_session_cannot_mute_a_channel_that_routes(
        self, monkeypatch, tmp_path
    ):
        """Suppression carries the same authority as arming, so it takes the owner too.

        `muted` names no routing field, so the refusal above never sees this request.
        What stops it is the armed-channel display fence, and that fence is armed by the
        caller the handler declares -- so a caller declared as privileged when it is not
        walks straight through it and silences a channel the owner armed.
        """
        state = _make_state(monkeypatch, tmp_path)
        async with TestClient(TestServer(_make_app(state))) as owner:
            assert (
                await owner.put(
                    "/api/notifications/channels/settings",
                    json={"channel": "system.cron", "deliver_to": ["slack"]},
                )
            ).status == 200

        app = _make_app(state, user=_NON_OWNER_SUBJECT)
        async with TestClient(TestServer(app)) as other:
            resp = await other.put(
                "/api/notifications/channels/settings",
                json={"channel": "system.cron", "muted": True},
            )
            status, payload = resp.status, await resp.json()
        assert status == 403, "a non-owner dashboard session silenced the owner's DMs"
        assert payload["code"] == "delivery_routing_owner_only"
        # Nothing written, and the route still delivers: a refusal that persisted the
        # mute would leave the channel silent while the caller reads 403.
        stored = state.notification_channel_settings.all_settings()["system.cron"]
        assert "muted" not in stored, f"the mute reached disk anyway: {stored}"
        assert state.notification_bridge.routes(
            {"channel": "system.cron", "priority": "critical"}
        ) == ("slack",)

    @pytest.mark.asyncio
    async def test_a_non_owner_dashboard_session_does_not_read_the_route_back_from_its_put(
        self, monkeypatch, tmp_path
    ):
        """The PUT's own reply is a carrier of the row, and it takes the owner question.

        A body naming only a channel is accepted and sets nothing, so neither refusal
        above sees it -- and the reply then hands back whatever is stored. Shaping that
        reply for an app alone leaves every other caller without routing authority
        reading the route it may not change.
        """
        state = _make_state(monkeypatch, tmp_path)
        async with TestClient(TestServer(_make_app(state))) as owner:
            assert (
                await owner.put(
                    "/api/notifications/channels/settings",
                    json={"channel": "system.cron", "deliver_to": ["slack"]},
                )
            ).status == 200

        app = _make_app(state, user=_NON_OWNER_SUBJECT)
        async with TestClient(TestServer(app)) as other:
            resp = await other.put(
                "/api/notifications/channels/settings",
                json={"channel": "system.cron"},
            )
            status, payload = resp.status, await resp.json()
        assert status == 200, "the no-op PUT itself is not the thing being refused"
        settings = payload["settings"]
        assert (
            "deliver_to" not in settings and "deliver_min_priority" not in settings
        ), f"the reply handed a non-owner the owner's route: {settings}"

        # Paired, so the strip cannot pass by blanking the owner's own reply.
        async with TestClient(TestServer(_make_app(state))) as owner:
            mine = (
                await (
                    await owner.put(
                        "/api/notifications/channels/settings",
                        json={"channel": "system.cron"},
                    )
                ).json()
            )["settings"]
        assert mine["deliver_to"] == ["slack"], f"the owner lost their own route: {mine}"

    @pytest.mark.asyncio
    async def test_an_owner_write_takes_over_an_apps_pending_override(self, monkeypatch, tmp_path):
        # The marker describes WHO the pending value came from, so an owner who sets
        # the pair themselves takes ownership of it and arming then leaves it alone.
        state = _make_state(monkeypatch, tmp_path)
        store = state.notification_channel_settings
        async with TestClient(TestServer(_make_app(state, app_name="tidy"))) as app_client:
            assert (
                await app_client.put(
                    "/api/notifications/channels/settings",
                    json={"channel": "system.cron", "muted": True},
                )
            ).status == 200
        async with TestClient(TestServer(_make_app(state))) as owner:
            assert (
                await owner.put(
                    "/api/notifications/channels/settings",
                    json={"channel": "system.cron", "muted": True},
                )
            ).status == 200
            assert (
                await owner.put(
                    "/api/notifications/channels/settings",
                    json={"channel": "system.cron", "deliver_to": ["slack"]},
                )
            ).status == 200
        assert store.get("system.cron")["muted"] is True

    def test_the_provenance_marker_is_withheld_from_readers(self, monkeypatch, tmp_path):
        # Internal bookkeeping, not a setting. A marker that leaked into the stored
        # row every reader sees would be a new API field nobody asked for.
        state = _make_state(monkeypatch, tmp_path)
        store = state.notification_channel_settings
        entry = store.update("system.cron", muted=True, app_caller="tidy")
        assert "display_override_by_app" not in entry
        assert "display_override_by_app" not in store.get("system.cron")
        assert "display_override_by_app" not in store.all_settings()["system.cron"]

    def test_an_app_clearing_mute_on_an_empty_channel_leaves_no_row(self, monkeypatch, tmp_path):
        # An app's `muted: false` is idempotent from the caller's side and has to be
        # idempotent in the store too. The clear takes the MARKING branch -- the request
        # does speak for `muted` -- and then the field is popped for being falsy. Without
        # retiring the mark it outlives the value it describes, and a mark ALONE keeps the
        # row non-empty: that persists a channel holding no settings and lists it as though
        # the user had configured something.
        state = _make_state(monkeypatch, tmp_path)
        store = state.notification_channel_settings
        entry = store.update("system.cron", muted=False, app_caller="tidy")
        assert "display_override_by_app" not in entry
        assert entry == {}, f"an app clearing mute left a phantom row behind: {entry}"
        assert (
            "system.cron" not in store.all_settings()
        ), "an app clearing mute persisted a channel that holds no settings"

    @pytest.mark.asyncio
    async def test_arming_keeps_a_mute_the_same_request_asks_for(self, monkeypatch, tmp_path):
        # Only values the request does not speak for are dropped, so an owner who
        # arms and mutes in one PUT gets the mute they asked for.
        state = _make_state(monkeypatch, tmp_path)
        store = state.notification_channel_settings
        async with TestClient(TestServer(_make_app(state))) as owner:
            assert (
                await owner.put(
                    "/api/notifications/channels/settings",
                    json={"channel": "system.cron", "deliver_to": ["slack"], "muted": True},
                )
            ).status == 200
        assert store.get("system.cron")["muted"] is True

    @pytest.mark.asyncio
    async def test_an_already_armed_channel_keeps_the_owners_settled_mute(
        self, monkeypatch, tmp_path
    ):
        # The discard is scoped to the TRANSITION. An owner's settled preference on
        # a channel that is already armed is theirs to keep, and a discard that
        # fired on every write would silently clear it.
        state = _make_state(monkeypatch, tmp_path)
        store = state.notification_channel_settings
        async with TestClient(TestServer(_make_app(state))) as owner:
            assert (
                await owner.put(
                    "/api/notifications/channels/settings",
                    json={"channel": "system.cron", "deliver_to": ["slack"], "muted": True},
                )
            ).status == 200
            assert (
                await owner.put(
                    "/api/notifications/channels/settings",
                    json={"channel": "system.cron", "deliver_min_priority": "all"},
                )
            ).status == 200
        stored = store.get("system.cron")
        assert stored["muted"] is True
        assert stored["deliver_min_priority"] == "all"

    @pytest.mark.asyncio
    async def test_an_owner_arming_during_the_apps_write_is_still_refused(
        self, monkeypatch, tmp_path
    ):
        """The decision has to be atomic with the write it guards.

        A check in the HANDLER reads the stored route lock-free and then awaits a
        separate `to_thread` write, so an owner PUT arming the route commits in
        between and the app's mute merges onto an armed entry -- leaving the channel
        muted AND armed, which `apply()` forces to `passive` so only an `all` floor
        routes it. The window is entered deterministically here: the owner's arm
        lands after the request is admitted and before the app's write takes the
        writer lock. The refusal is decided inside that lock, against the entry the
        write merges onto, so it sees the arm.
        """
        state = _make_state(monkeypatch, tmp_path)
        store = state.notification_channel_settings
        real_update = store.update

        def _arm_then_update(channel, **kwargs):
            real_update("system.cron", deliver_to=["slack"])
            return real_update(channel, **kwargs)

        monkeypatch.setattr(store, "update", _arm_then_update)
        async with TestClient(TestServer(_make_app(state, app_name="rogue"))) as client:
            resp = await client.put(
                "/api/notifications/channels/settings",
                json={"channel": "system.cron", "muted": True},
            )
        assert resp.status == 403
        stored = store.all_settings()["system.cron"]
        assert stored["deliver_to"] == ["slack"]
        assert "muted" not in stored

    @pytest.mark.asyncio
    async def test_the_owner_can_still_mute_a_channel_that_routes(self, monkeypatch, tmp_path):
        # The refusal is about WHO, not about the field: muting an armed channel
        # is the owner's call and still silences its DMs, which is what mute means.
        state = _make_state(monkeypatch, tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            assert (
                await client.put(
                    "/api/notifications/channels/settings",
                    json={"channel": "system.cron", "deliver_to": ["slack"]},
                )
            ).status == 200
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

    @pytest.mark.asyncio
    async def test_an_app_tokens_mute_of_an_armed_channel_is_refused(self, monkeypatch, tmp_path):
        # A mute-only PUT names no routing key, so the routing-field refusal above
        # does not see it. It still reaches the bridge: `apply()` rewrites the
        # note's priority and the floor ranks that EFFECTIVE value, so an app's
        # mute silences a DM the owner armed without the app ever naming a routing
        # field. The display pair is refused while a route is armed, which is why
        # this request is a 403 rather than a response shaping.
        state = _make_state(monkeypatch, tmp_path)
        state.notification_channel_settings.update(
            "system.cron", deliver_to=["slack"], deliver_min_priority="all"
        )
        async with TestClient(TestServer(_make_app(state, app_name="tidy"))) as client:
            resp = await client.put(
                "/api/notifications/channels/settings",
                json={"channel": "system.cron", "muted": True},
            )
        assert resp.status == 403
        # The owner's route survives, and so does the absence of the app's mute.
        stored = state.notification_channel_settings.all_settings()["system.cron"]
        assert stored["deliver_to"] == ["slack"]
        assert "muted" not in stored

    @pytest.mark.asyncio
    async def test_an_app_tokens_put_reply_does_not_carry_the_owners_routing(
        self, monkeypatch, tmp_path
    ):
        # The reply withholding sits behind that 403 as defence in depth, so it is
        # pinned on the request that still reaches it: one naming neither a routing
        # key nor the display pair. `update()` merges onto the stored entry, so the
        # reply really does carry the armed route unless it is withheld -- this is
        # not vacuous.
        state = _make_state(monkeypatch, tmp_path)
        state.notification_channel_settings.update(
            "system.cron", deliver_to=["slack"], deliver_min_priority="all"
        )
        async with TestClient(TestServer(_make_app(state, app_name="tidy"))) as client:
            resp = await client.put(
                "/api/notifications/channels/settings",
                json={"channel": "system.cron"},
            )
            status, payload = resp.status, await resp.json()
        assert status == 200
        assert "deliver_to" not in payload["settings"]
        assert "deliver_min_priority" not in payload["settings"]
        # Withholding is a response shaping, not a mutation: the owner's route
        # must survive an app's request.
        assert state.notification_channel_settings.all_settings()["system.cron"]["deliver_to"] == [
            "slack"
        ]

    @pytest.mark.asyncio
    async def test_the_owners_own_mute_reply_still_carries_the_routing(self, monkeypatch, tmp_path):
        # Paired with the test above so the strip cannot pass by withholding from
        # everyone -- the Settings UI reads its armed route back from this reply.
        state = _make_state(monkeypatch, tmp_path)
        state.notification_channel_settings.update("system.cron", deliver_to=["slack"])
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.put(
                "/api/notifications/channels/settings",
                json={"channel": "system.cron", "muted": True},
            )
            payload = await resp.json()
        assert payload["settings"]["deliver_to"] == ["slack"]

    def test_the_guarded_key_set_is_declared_once(self):
        # Every carrier of the stored row derives its check from this set, so a
        # third routing key added later is refused and withheld without touching
        # any of them.
        from kiro_crew.notifications.settings import DELIVERY_SETTING_KEYS

        assert DELIVERY_SETTING_KEYS == frozenset({"deliver_to", "deliver_min_priority"})

    def test_the_ws_carrier_uses_that_same_set_rather_than_a_copy(self):
        # Identity, not equality: a copied set in the WS module would compare
        # equal today and silently diverge the day a key is added, which is the
        # failure the single declaration exists to prevent.
        from kiro_crew.dashboard import ws_event_scope
        from kiro_crew.notifications.settings import DELIVERY_SETTING_KEYS

        assert ws_event_scope.DELIVERY_SETTING_KEYS is DELIVERY_SETTING_KEYS


class TestTheWsFrameWithholdsRoutingFromApps:
    """The settings frame is the row's third carrier, and the one an app reaches
    without calling anything.

    ``notification_channel_settings`` is attributable by channel prefix, so an
    app scoped to its own channel receives this frame whenever ANY writer changes
    a setting on it -- including the owner's own PUT, which the handler-side
    withholding cannot reach. The strip therefore belongs at the per-client
    serialization chokepoint, which is also where the owner's dashboard is
    exempted so its Settings page still renders the armed route.
    """

    _FRAME = "notification_channel_settings"
    _DATA = {
        "channel": "tidy.build",
        "settings": {"muted": True, "deliver_to": ["slack"], "deliver_min_priority": "all"},
    }

    def _serialize(self, state, ws):
        return json.loads(
            state._serialize_for_client(
                ws, self._FRAME, self._DATA, json.dumps({"type": self._FRAME, "data": self._DATA})
            )
        )

    def test_an_app_scoped_client_does_not_receive_the_owners_routing(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        frame = self._serialize(state, {"_app": "tidy"})
        assert frame["type"] == self._FRAME
        assert "deliver_to" not in frame["data"]["settings"]
        assert "deliver_min_priority" not in frame["data"]["settings"]
        # The app keeps what it is entitled to: which channel, and its mute.
        assert frame["data"]["channel"] == "tidy.build"
        assert frame["data"]["settings"]["muted"] is True

    def test_the_owners_dashboard_still_receives_the_whole_row(self, monkeypatch, tmp_path):
        # Paired with the test above: a strip that applied to every client would
        # blank the Settings page's own route picker after any write.
        state = _make_state(monkeypatch, tmp_path)
        frame = self._serialize(state, {"_is_owner": True, "_is_dashboard_user": True})
        assert frame["data"]["settings"]["deliver_to"] == ["slack"]
        assert frame["data"]["settings"]["deliver_min_priority"] == "all"

    def test_a_non_owner_dashboard_socket_does_not_receive_the_owners_routing(
        self, monkeypatch, tmp_path
    ):
        """The carrier no client has to ask for, and the widest way to reach it.

        This socket holds no app scope and makes no request: it is simply open while
        the owner edits a setting. Being a dashboard user is not being the owner --
        that flag is set from the absence of an app claim, which an allow-listed
        messaging user's session satisfies -- so keying the exemption on it hands the
        route to a client that never asked for anything.
        """
        state = _make_state(monkeypatch, tmp_path)
        frame = self._serialize(state, {"_is_dashboard_user": True})
        settings = frame["data"]["settings"]
        assert (
            "deliver_to" not in settings and "deliver_min_priority" not in settings
        ), f"a non-owner socket received the owner's routing: {settings}"
        # Still the frame it is entitled to, so this is a withholding of two fields
        # rather than a dropped event.
        assert frame["type"] == self._FRAME
        assert frame["data"]["channel"] == "tidy.build"
        assert settings["muted"] is True

    def test_the_socket_owner_flag_comes_from_the_owner_predicate(self):
        """The flag the chokepoint reads is set from ownership, not re-derived.

        Asserted structurally because the value is written in the websocket handler
        and read three modules away: a flag assigned from anything but the predicate
        already resolved there would satisfy every behavioural test above while
        admitting the wrong principal in production.
        """
        import ast
        import pathlib

        source = pathlib.Path("src/kiro_crew/dashboard/ws.py").read_text(encoding="utf-8")
        assert "owner_request = is_owner_dashboard_request(request)" in source
        tree = ast.parse(source)
        assigned = [
            ast.unparse(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Subscript)
            and ast.unparse(target).replace('"', "'") == "ws['_is_owner']"
        ]
        assert assigned == ["owner_request"], f"_is_owner is not the owner predicate: {assigned}"

    def test_an_unreadable_payload_yields_no_settings_rather_than_the_original(self):
        # The gate must not widen on a shape it cannot read. Returning the object
        # unchanged on the default branch is how a strip becomes a pass-through.
        from kiro_crew.dashboard.ws_event_scope import channel_settings_for_app

        assert channel_settings_for_app(["not", "a", "mapping"]) == {"channel": "", "settings": {}}
        assert channel_settings_for_app({"channel": "x.y", "settings": None}) == {
            "channel": "x.y",
            "settings": {},
        }


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
