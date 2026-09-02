"""Tests for ``dashboard.session_card_source_links`` -- the sidebar chip switch.

The PR/issue chip strip on every session card used to ship unconditionally: two
fields in ``_ChatSlot.to_dict`` and a periodic credentialed status refresh fed by
``DashboardState.source_link_urls``, with nothing in between consulting config.
These tests pin both halves of the switch -- the payload AND the refresh feed --
plus the default, because a default that ever reads false would silently strip
chips from every existing install.

The switch is read from the same off-loop snapshot as the self-managed host
allowlists (``ensure_gitlab_hosts_loaded``), so the no-config-read-on-the-loop
invariant is pinned here too: this value is consulted on every slots push.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

import kiro_crew.dashboard.handlers.source_providers as sp
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.chat_handlers import api_chat_slot_source_links
from kiro_crew.dashboard.state import DashboardState, _ChatSlot

PRS = [f"https://github.com/acme/widgets/pull/{n}" for n in (11, 12)]
ISSUES = [f"https://github.com/acme/widgets/issues/{n}" for n in (21, 22)]


def _slot(key: str = "s1") -> _ChatSlot:
    slot = _ChatSlot(key)
    slot.append("assistant", "\n".join([*PRS, *ISSUES]), ts="t1")
    return slot


def _state(tmp_path, monkeypatch) -> DashboardState:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    return DashboardState(
        sessions=MagicMock(count=0),
        crons=MagicMock(),
        lessons=MagicMock(),
        start_time=0.0,
    )


def _set_config(monkeypatch, value: bool) -> None:
    """Seed the cached snapshot the sync getter reads.

    Patches the snapshot in ``source_providers``, which is where it lives: the
    getter is cache-only by design, so there is no config file to point at. The
    value normally arrives from ``_load_source_link_settings`` in a worker
    thread.
    """
    monkeypatch.setattr(sp, "_session_card_chips_snapshot", value)


class TestSnapshotRead:
    def test_cold_snapshot_means_on(self) -> None:
        """The strip predates the switch, so the value a process starts with --
        before any refresh has landed -- has to keep it rendering. The host
        allowlists in the same snapshot start EMPTY (fail closed); this one starts
        true (fails open) because the failure modes are opposite."""
        assert sp._session_card_chips_snapshot is True

    def test_reads_the_snapshot(self, monkeypatch) -> None:
        _set_config(monkeypatch, False)
        assert sp.session_card_source_links_enabled() is False
        _set_config(monkeypatch, True)
        assert sp.session_card_source_links_enabled() is True

    def test_never_reads_config_on_the_event_loop(self, monkeypatch) -> None:
        """Companion to test_gitlab_allowlist_never_reads_config_on_the_event_loop.
        This runs once per slots push, and ``KiroCrewConfig.load()`` stats, reads,
        parses and validates config files -- so the getter must not reach it."""
        monkeypatch.setattr(
            sp,
            "_load_source_link_settings",
            lambda: pytest.fail("config read from the sync getter"),
        )
        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load",
            staticmethod(lambda *a, **k: pytest.fail("config load from the sync getter")),
        )
        assert sp.session_card_source_links_enabled() is True

    def test_a_flip_bumps_the_generation_so_open_tabs_are_pushed(self, monkeypatch) -> None:
        """The owner websocket's refresh round pushes a fresh slots payload when
        the shared generation moves. Without the bump the chips would keep
        rendering until some unrelated message mutation triggered a push."""
        _set_config(monkeypatch, True)
        monkeypatch.setattr(sp, "_gitlab_hosts_generation", 7)

        sp._publish_session_card_chips(False)
        assert sp.session_card_source_links_enabled() is False
        assert sp.gitlab_hosts_generation() == 8

        # Idempotent: republishing the same value is not a change to push.
        sp._publish_session_card_chips(False)
        assert sp.gitlab_hosts_generation() == 8

    def test_an_unreadable_config_loads_as_on(self, monkeypatch) -> None:
        """A torn or unreadable config must not be indistinguishable from "the
        user said off" -- the hosts in the same tuple fail closed, this does not."""
        monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load",
            staticmethod(lambda *a, **k: (_ for _ in ()).throw(OSError("torn read"))),
        )
        gitlab, jira, chips = sp._load_source_link_settings()
        assert (gitlab, jira) == (frozenset(), frozenset())
        assert chips is True

    @pytest.mark.asyncio
    async def test_a_write_during_an_in_flight_refresh_is_not_overwritten(
        self, monkeypatch
    ) -> None:
        """The switch has two writers: the config PUT (which publishes at write
        time so the click is immediate) and the 30s poll. A poll already in flight
        is holding the older reading, and publishing that after the write would
        resume the chips -- and the credentialed polling behind them -- for another
        full interval.

        ``publish_session_card_chips_now`` takes the lock the refresh holds across
        its threaded load, so the write simply lands last. Exercised through that
        function, which is the path the handler uses.
        """
        monkeypatch.setattr(sp, "_session_card_chips_snapshot", True)
        monkeypatch.setattr(sp, "_gitlab_hosts_loaded_at", 0.0)
        monkeypatch.setattr(sp, "_gitlab_hosts_lock", asyncio.Lock())
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = asyncio.Event()

        def slow_load() -> tuple[frozenset[str], frozenset[str], bool]:
            # Runs in a worker thread: marshal the Event back onto the loop.
            loop.call_soon_threadsafe(started.set)
            asyncio.run_coroutine_threadsafe(release.wait(), loop).result(timeout=5)
            # The reading this load is holding: chips still ON.
            return frozenset(), frozenset(), True

        monkeypatch.setattr(sp, "_load_source_link_settings", slow_load)

        refresh = loop.create_task(sp.ensure_gitlab_hosts_loaded())
        await started.wait()
        # The user turns the chips off while the load is in flight.
        write = loop.create_task(sp.publish_session_card_chips_now(False))
        await asyncio.sleep(0)
        release.set()
        await refresh
        await write

        assert sp.session_card_source_links_enabled() is False


class TestSlotPayload:
    def test_on_by_default_serializes_the_strip(self) -> None:
        payload = _slot().to_dict()
        assert [link["url"] for link in payload["source_links"]] == [
            *reversed(PRS),
            *reversed(ISSUES),
        ]
        assert payload["source_links_total"] == 4

    def test_off_empties_both_fields_rather_than_dropping_them(self, monkeypatch) -> None:
        """The keys stay present: a client that reads ``source_links_total``
        without a guard would render ``+undefined`` if the field vanished."""
        _set_config(monkeypatch, False)
        payload = _slot().to_dict()
        assert payload["source_links"] == []
        assert payload["source_links_total"] == 0

    def test_off_skips_extraction_entirely(self, monkeypatch) -> None:
        """Not just the two fields -- the transcript scan behind them is the cost
        the switch exists to remove, so it must not run at all."""
        _set_config(monkeypatch, False)
        slot = _slot()
        with patch.object(
            _ChatSlot, "_pr_source_links", side_effect=AssertionError("extracted while off")
        ):
            payload = slot.to_dict()
        assert payload["source_links"] == []


class TestSerializeSlots:
    def test_config_off_strips_every_slot(self, tmp_path, monkeypatch) -> None:
        state = _state(tmp_path, monkeypatch)
        state._slots = {"s1": _slot("s1"), "s2": _slot("s2")}

        _set_config(monkeypatch, True)
        assert [len(p["source_links"]) for p in state.serialize_slots()] == [4, 4]

        _set_config(monkeypatch, False)
        assert [p["source_links"] for p in state.serialize_slots()] == [[], []]
        assert [p["source_links_total"] for p in state.serialize_slots()] == [0, 0]

    def test_the_switch_is_resolved_where_it_is_used(self, tmp_path, monkeypatch) -> None:
        """No flag is threaded down from the fan-out point. The getter reads an
        in-memory snapshot, so there is no per-slot cost to amortize -- and
        publication happens on the loop, so a synchronous slot loop cannot observe
        a flip mid-push and serialize a mixed payload."""
        state = _state(tmp_path, monkeypatch)
        state._slots = {f"s{n}": _slot(f"s{n}") for n in range(5)}
        _set_config(monkeypatch, False)
        assert all(p["source_links"] == [] for p in state.serialize_slots())

        import inspect

        for fn in (
            _ChatSlot.to_dict,
            DashboardState.serialize_slot,
            DashboardState.serialize_slots,
        ):
            assert "include_source_links" not in inspect.signature(fn).parameters

    def test_a_single_slot_follows_the_switch_too(self, tmp_path, monkeypatch) -> None:
        """``serialize_slot`` has callers of its own, so the gate cannot live only
        in the multi-slot path."""
        state = _state(tmp_path, monkeypatch)
        slot = _slot()
        _set_config(monkeypatch, False)
        assert state.serialize_slot(slot)["source_links"] == []
        _set_config(monkeypatch, True)
        assert len(state.serialize_slot(slot)["source_links"]) == 4


class TestCheckRefreshFeed:
    """Gating the payload alone would leave the provider polling for chips nobody
    renders -- the reporter's point 5, and the only cost this switch can remove
    that a CSS rule could not."""

    def test_urls_are_withheld_while_off(self, tmp_path, monkeypatch) -> None:
        state = _state(tmp_path, monkeypatch)
        state._slots = {"s1": _slot()}

        _set_config(monkeypatch, True)
        assert state.source_link_urls() == list(reversed(PRS))
        assert state.source_link_urls_for_slot("s1") == list(reversed(PRS))

        _set_config(monkeypatch, False)
        assert state.source_link_urls() == []
        assert state.source_link_urls_for_slot("s1") == []

    def test_the_turn_boundary_refresh_spawns_no_provider_call(self, tmp_path, monkeypatch) -> None:
        """``refresh_slot_source_status`` is the owner-gated turn-boundary hook;
        with no URLs it must return before reaching the provider."""
        state = _state(tmp_path, monkeypatch)
        state._slots = {"s1": _slot()}
        state._owner_ws_clients = {MagicMock()}
        _set_config(monkeypatch, False)

        with patch(
            "kiro_crew.dashboard.handlers.source_providers.request_check_refresh_now"
        ) as refresh:
            state.refresh_slot_source_status("s1")
        refresh.assert_not_called()


def _request(slot_key: str, slots: dict):
    request = MagicMock(spec=web.Request)
    request.method = "GET"
    request.match_info = {"slot": slot_key}
    request.get = lambda key, default=None: default
    state = MagicMock()
    state._slots = slots
    request.app = {"state": state}
    return request


class TestExpandEndpoint:
    @pytest.mark.asyncio
    async def test_unknown_slot_404s(self) -> None:
        """The overflow expand is deliberately NOT gated on the switch: its only
        caller is a pill that exists while the strip renders, and an app token that
        owns the slot can already read the messages these URLs are extracted from.
        So this pins the one refusal it does owe -- "gone" is a 404, never an empty
        strip, which would report a deleted session as linkless."""
        with (
            patch(
                "kiro_crew.dashboard.handlers.source_providers.ensure_gitlab_hosts_loaded",
                return_value=None,
            ),
            patch("kiro_crew.dashboard.chat_handlers.sel"),
        ):
            resp = await api_chat_slot_source_links(_request("nope", {"s1": _slot()}))

        assert resp.status == 404


@pytest.fixture()
def cfg_file(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{}", encoding="utf-8")
    with patch("kiro_crew.config.loader.config_path", return_value=p):
        yield p


class TestConfigField:
    def test_default_is_on_so_no_install_loses_its_chips(self) -> None:
        assert KiroCrewConfig().dashboard.session_card_source_links is True

    def test_save_load_round_trip(self, cfg_file) -> None:
        cfg = KiroCrewConfig()
        cfg.dashboard.session_card_source_links = False
        cfg.save()

        raw = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert raw["dashboard"]["session_card_source_links"] is False
        assert KiroCrewConfig.load().dashboard.session_card_source_links is False

    def test_a_hand_edited_non_bool_stays_on(self, cfg_file) -> None:
        cfg_file.write_text(
            json.dumps({"dashboard": {"session_card_source_links": "no"}}), encoding="utf-8"
        )
        assert KiroCrewConfig.load().dashboard.session_card_source_links is True

    def test_carried_into_the_generated_schema(self) -> None:
        """The settings UI reads /api/config/schema, so the label and help have to
        travel with the field rather than being re-typed in the panel."""
        from kiro_crew.config.schema import JSON_SCHEMA

        node = JSON_SCHEMA["properties"]["dashboard"]["properties"]["session_card_source_links"]
        assert node["type"] == "boolean"
        assert node["default"] is True
        assert node["x-meta"]["label"]
        assert node["x-meta"]["help"]


@pytest.fixture()
def mock_sel():
    try:
        import kiro_crew.dashboard.handlers  # noqa: F401
    except ImportError:
        pytest.skip("dashboard handler deps not available locally")
    m = MagicMock()
    m.log_tool_invocation = MagicMock()
    with patch("kiro_crew.dashboard.handlers.sel", return_value=m):
        yield m


class _OwnerState:
    """Minimal ``request.app["state"]`` for the owner-gated PUT.

    ``owner_id = ""`` is the standalone-local shape the gate accepts (see
    ``dashboard_owner_helpers``); a bare ``MagicMock`` answers 401 because its
    auto-attribute ``owner_id`` is not that empty string. ``push_slots_update``
    records instead of broadcasting.
    """

    owner_id = ""

    def __init__(self, pushes: list[int]) -> None:
        self._pushes = pushes

    def push_slots_update(self) -> None:
        self._pushes.append(1)


@pytest.fixture()
def handler_app(cfg_file, mock_sel):
    from kiro_crew.dashboard.handlers.files import api_dashboard_config

    app = web.Application()
    app.router.add_put("/api/dashboard/config", api_dashboard_config)
    app.router.add_get("/api/dashboard/config", api_dashboard_config)
    return as_owner(app)


class TestConfigEndpoint:
    """``/api/dashboard/config`` validates each field explicitly and echoes a
    fixed dict, so a new field is wired in THREE places (PUT allowlist,
    validation branch, GET response). Missing any one leaves the toggle unable to
    persist -- and it fails silently, which is why all three are pinned."""

    @pytest.mark.asyncio
    async def test_put_persists_off(self, handler_app, cfg_file) -> None:
        async with TestClient(TestServer(handler_app)) as client:
            resp = await client.put(
                "/api/dashboard/config", json={"session_card_source_links": False}
            )
            assert resp.status == 200
        assert KiroCrewConfig.load().dashboard.session_card_source_links is False

    @pytest.mark.asyncio
    async def test_put_rejects_a_non_bool(self, handler_app, cfg_file) -> None:
        async with TestClient(TestServer(handler_app)) as client:
            resp = await client.put(
                "/api/dashboard/config", json={"session_card_source_links": "off"}
            )
            assert resp.status == 400
            body = await resp.json()
        assert "boolean" in body["error"]
        assert body["code"] == "invalid_session_card_source_links"

    @pytest.mark.asyncio
    async def test_get_echoes_the_stored_value(self, handler_app, cfg_file) -> None:
        """Without this the toggle renders permanently on after a successful
        write, because the panel re-reads its state from this response."""
        cfg_file.write_text(
            json.dumps({"dashboard": {"session_card_source_links": False}}), encoding="utf-8"
        )
        async with TestClient(TestServer(handler_app)) as client:
            resp = await client.get("/api/dashboard/config")
            assert resp.status == 200
            body = await resp.json()
        assert body["session_card_source_links"] is False

    @pytest.mark.asyncio
    async def test_a_write_publishes_the_switch_and_pushes_the_slots(
        self, handler_app, cfg_file, monkeypatch
    ) -> None:
        """The refresh TTL is 30s. Waiting for it would leave the sidebar
        rendering chips for half a minute after an explicit click -- the switch
        acknowledges itself instantly and nothing happens, which reads as broken.
        The handler already knows the value, so it publishes it and pushes.
        """
        monkeypatch.setattr(sp, "_session_card_chips_snapshot", True)
        pushes: list[int] = []
        handler_app["state"] = _OwnerState(pushes)

        async with TestClient(TestServer(handler_app)) as client:
            resp = await client.put(
                "/api/dashboard/config", json={"session_card_source_links": False}
            )
            assert resp.status == 200

        assert sp.session_card_source_links_enabled() is False
        assert pushes == [1]

    @pytest.mark.asyncio
    async def test_a_write_of_another_key_does_not_touch_the_switch(
        self, handler_app, cfg_file, monkeypatch
    ) -> None:
        """Only a body that carried the key republishes. A sibling toggle must not
        reach into this snapshot, or a `link_previews` save would resurrect chips
        the user turned off in another tab.

        Spies on the PUBLISHER rather than only on the resulting snapshot: the
        publish path is deliberately wrapped in a swallowing ``except`` (a saved
        setting must not be reported as unsaved), so an unguarded call that raised
        would leave the snapshot untouched too and an outcome-only assertion would
        pass vacuously.
        """
        monkeypatch.setattr(sp, "_session_card_chips_snapshot", False)
        published: list[bool] = []
        monkeypatch.setattr(sp, "_publish_session_card_chips", lambda v: published.append(v))
        pushes: list[int] = []
        handler_app["state"] = _OwnerState(pushes)

        async with TestClient(TestServer(handler_app)) as client:
            resp = await client.put("/api/dashboard/config", json={"link_previews": True})
            assert resp.status == 200

        assert published == []
        assert pushes == []
        assert sp.session_card_source_links_enabled() is False

    @pytest.mark.asyncio
    async def test_the_publish_path_is_reached_not_merely_swallowed(
        self, handler_app, cfg_file, monkeypatch
    ) -> None:
        """Companion to the two above: pins that the handler really calls the
        publisher with the written value, so neither test can be satisfied by an
        exception on the way there."""
        published: list[bool] = []
        monkeypatch.setattr(sp, "_publish_session_card_chips", lambda v: published.append(v))
        handler_app["state"] = _OwnerState([])

        async with TestClient(TestServer(handler_app)) as client:
            resp = await client.put(
                "/api/dashboard/config", json={"session_card_source_links": False}
            )
            assert resp.status == 200

        assert published == [False]
