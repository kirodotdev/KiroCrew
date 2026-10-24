"""When DashboardState refreshes source-link chip status, and who hears of it.

This file pins the periodic and turn-boundary refresh triggers. The periodic driver
an owner WebSocket connection starts covers the chip URLs the sidebar renders,
paces its rounds by the status TTL, rotates which URL leads each round and survives
a round that raises; it warms the GitLab allowlist before the first slots frame and
pushes slots when the allowlist generation moves. An app token or a non-owner
dashboard user never starts it. The turn-boundary refresh reads only the slot
whose turn ended and runs only while an owner window is open, and its status delta
reaches owner sockets alone through the sink the dashboard wires and removes on
shutdown. Which audience each status frame reaches, and that only an owner's
connection refreshes it at connect, live in ``test_dashboard_state_ws.py``, and the
shared fixtures in ``dashboard_state_ws_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest
from dashboard_state_ws_helpers import (  # noqa: F401 -- fixtures are requested by name
    state_fixture,
    sync_event_loop,
)

from kiro_crew.dashboard.state import DashboardState
from kiro_crew.testing.wait import await_until


class TestPeriodicCheckStatusRefresh:
    """Regression: sidebar PR chip status must not freeze at connect time.

    ``push_slots_update`` serves *cached* check status but never schedules
    refreshes, so before the periodic owner-WS driver existed the cache was
    only populated at WS-connect / slots-GET time — a PR merged after page
    load never gained its merge icon until a full reload.
    """

    def test_ttl_alias_matches_cache_ttl(self) -> None:
        from kiro_crew.dashboard.handlers import source_providers

        assert source_providers.CHECK_STATUS_TTL_SECS == source_providers._CHECK_TTL_SECS

    def test_source_link_urls_spans_slots_and_caps_at_serialized_count(
        self, state: DashboardState
    ) -> None:
        slot_a = state.get_or_create_slot("chat-a")
        for n in (1, 2, 3, 4):
            slot_a.append(
                "assistant", f"see https://github.com/acme/repo/pull/{n}", broadcast=False
            )
        slot_b = state.get_or_create_slot("chat-b")
        slot_b.append("assistant", "and https://github.com/acme/other/pull/9", broadcast=False)

        urls = state.source_link_urls()

        # Capped at the serialized chip count per slot — refreshing links the
        # sidebar never renders would waste provider quota — and aggregated
        # across every slot so background sessions stay fresh too. WHICH links
        # survive the cap is recency-ordered (newest mention first), because the
        # refresher and the serializer share `_budgeted_source_links` and must
        # keep agreeing on exactly the chips the sidebar renders.
        assert urls == [
            "https://github.com/acme/repo/pull/4",
            "https://github.com/acme/repo/pull/3",
            "https://github.com/acme/repo/pull/2",
            "https://github.com/acme/other/pull/9",
        ]

    @pytest.mark.asyncio
    async def test_owner_ws_loop_schedules_ttl_paced_refreshes(self, monkeypatch) -> None:
        from kiro_crew.dashboard import ws as dashboard_ws
        from kiro_crew.dashboard.handlers import source_providers

        url = "https://github.com/acme/repo/pull/248"
        state = MagicMock()
        state.owner_id = "U_OWNER"
        state.serialize_slots.return_value = []
        state._yolo = False
        state.source_link_urls.return_value = [url]

        class Request(dict):
            def __init__(self) -> None:
                super().__init__({"user": "U_OWNER", "app": ""})
                # Mirror the auth middleware: it sets this POSITIVE flag so the
                # WS layer never infers trust from a falsy app claim.
                self.setdefault("is_dashboard_user", not self.get("app"))
                self.app = {"state": state}

        refreshed = asyncio.Event()
        refresh_calls: list[tuple] = []

        def refresh(urls, on_update=None):
            refresh_calls.append((urls, on_update))
            refreshed.set()

        class FakeWebSocket:
            def __init__(self) -> None:
                self.closed = False
                self.sent: list[dict] = []
                # api_ws stores scope state on the socket via item assignment;
                # flag as a dashboard user so the WS scope gate passes through.
                self._flags: dict = {"_is_dashboard_user": True}

            def __setitem__(self, key: str, value) -> None:
                self._flags[key] = value

            def __getitem__(self, key: str):
                return self._flags[key]

            def get(self, key: str, default=None):
                return self._flags.get(key, default)

            async def prepare(self, request) -> None:
                return None

            async def send_json(self, payload: dict) -> None:
                self.sent.append(payload)

            async def send_str(self, payload: str) -> None:
                # Connect snapshot sends a pre-dumped string (offender-note
                # seam); parse back so assertions keep reading dict frames.
                self.sent.append(json.loads(payload))

            def __aiter__(self):
                return self

            async def __anext__(self):
                # Hold the connection open until the periodic loop has fired
                # once, then end the handler (which cancels the loop task).
                # flake-ok: pre-existing unbounded wait that holds the fake socket open, moved verbatim; the await_until around api_ws bounds the test
                await refreshed.wait()
                raise StopAsyncIteration

        fake_ws = FakeWebSocket()
        monkeypatch.setattr(dashboard_ws, "_check_ws_origin", lambda request: None)
        monkeypatch.setattr(dashboard_ws.web, "WebSocketResponse", lambda **kwargs: fake_ws)
        monkeypatch.setattr(source_providers, "CHECK_STATUS_TTL_SECS", 0.01)
        monkeypatch.setattr(source_providers, "schedule_check_refresh", refresh)

        await await_until(asyncio.ensure_future(dashboard_ws.api_ws(Request())))  # type: ignore[arg-type]

        # The loop fired after one TTL tick with the visible chip URLs and the
        # broadcast callback (which pushes only on actual status change).
        assert refresh_calls
        assert refresh_calls[0] == ([url], state.push_slots_update)

    @pytest.mark.asyncio
    async def test_owner_ws_loop_pushes_slots_when_allowlist_generation_changes(
        self, monkeypatch
    ) -> None:
        """An operator adding or revoking a self-managed host changes which links
        are chips at all, and slot extraction is synchronous -- so the periodic
        loop must push explicitly instead of waiting for message activity."""
        from kiro_crew.dashboard import ws as dashboard_ws
        from kiro_crew.dashboard.handlers import source_providers
        from kiro_crew.platform import governance_profiles

        state = MagicMock()
        state.owner_id = "U_OWNER"
        state.serialize_slots.return_value = []
        state._yolo = False
        state.source_link_urls.return_value = []

        pushed = asyncio.Event()
        state.push_slots_update.side_effect = lambda *a, **k: pushed.set()

        calls = {"n": 0}

        async def fake_ensure() -> frozenset:
            calls["n"] += 1
            # Change the generation only on the periodic round, not the warm-up.
            if calls["n"] == 2:
                source_providers._publish_provider_hosts(
                    frozenset({"gitlab.acme.internal"}), frozenset()
                )
            return frozenset()

        class Request(dict):
            def __init__(self) -> None:
                super().__init__({"user": "U_OWNER", "app": ""})
                # Mirror the auth middleware: it sets this POSITIVE flag so the
                # WS layer never infers trust from a falsy app claim.
                self.setdefault("is_dashboard_user", not self.get("app"))
                self.app = {"state": state}

        class FakeWebSocket:
            def __init__(self) -> None:
                self.closed = False
                self.sent: list[dict] = []
                # api_ws stores scope state on the socket via item assignment;
                # flag as a dashboard user so the WS scope gate passes through.
                self._flags: dict = {"_is_dashboard_user": True}

            def __setitem__(self, key: str, value) -> None:
                self._flags[key] = value

            def __getitem__(self, key: str):
                return self._flags[key]

            def get(self, key: str, default=None):
                return self._flags.get(key, default)

            async def prepare(self, request) -> None:
                return None

            async def send_json(self, payload: dict) -> None:
                self.sent.append(payload)

            async def send_str(self, payload: str) -> None:
                # Connect snapshot sends a pre-dumped string (offender-note
                # seam); parse back so assertions keep reading dict frames.
                self.sent.append(json.loads(payload))

            def __aiter__(self):
                return self

            async def __anext__(self):
                # flake-ok: pre-existing unbounded wait that holds the fake socket open, moved verbatim; the await_until around api_ws bounds the test
                await pushed.wait()
                raise StopAsyncIteration

        monkeypatch.setattr(source_providers, "_gitlab_hosts_snapshot", frozenset())
        monkeypatch.setattr(source_providers, "_gitlab_hosts_loaded_at", 0.0)
        monkeypatch.setattr(source_providers, "_gitlab_hosts_generation", 0)
        monkeypatch.setattr(dashboard_ws, "_check_ws_origin", lambda request: None)
        monkeypatch.setattr(dashboard_ws.web, "WebSocketResponse", lambda **kwargs: FakeWebSocket())
        monkeypatch.setattr(source_providers, "ensure_gitlab_hosts_loaded", fake_ensure)
        monkeypatch.setattr(source_providers, "CHECK_STATUS_TTL_SECS", 0.01)
        # The governance watch also pushes slots when real profile files change,
        # which other tests on the same xdist worker do; pin it so the only push
        # left is the host-allowlist one this test is about.
        monkeypatch.setattr(governance_profiles, "poll_profiles_fresh", lambda: None)
        monkeypatch.setattr(governance_profiles, "governance_answer_generation", lambda: 0)

        await await_until(asyncio.ensure_future(dashboard_ws.api_ws(Request())))  # type: ignore[arg-type]

        assert calls["n"] >= 2
        state.push_slots_update.assert_called_with()

    @pytest.mark.asyncio
    async def test_slots_broadcast_carries_gitlab_hosts_generation(
        self, state: DashboardState, monkeypatch
    ) -> None:
        """The WS `slots` envelope is rebuilt key-by-key in `_broadcast`, so an
        unforwarded field is silently dropped — which would leave the client with
        no way to notice an allowlist change now that polling was removed."""
        from kiro_crew.dashboard.handlers import source_providers

        sent: list[str] = []

        class FakeWs:
            closed = False

            # Dashboard-user flag so the WS scope gate passes through; this
            # test targets the slots envelope, not per-app filtering.
            def get(self, key: str, default=None):
                return True if key == "_is_dashboard_user" else default

            def send_str(self, msg: str):
                sent.append(msg)

                async def _noop() -> None:
                    return None

                return _noop()

        state._ws_clients = [FakeWs()]  # type: ignore[assignment]
        monkeypatch.setattr(source_providers, "_gitlab_hosts_generation", 7)

        state.push_slots_update()

        assert sent, "no slots frame was broadcast"
        payload = json.loads(sent[-1])
        assert payload["type"] == "slots"
        assert payload["gitlabHostsGeneration"] == 7

    @pytest.mark.asyncio
    async def test_ws_warms_gitlab_allowlist_before_first_serialization(self, monkeypatch) -> None:
        """Slot source-link extraction is synchronous and cannot load the
        allowlist, so a self-hosted MR chip would be missing from the very first
        sidebar push unless the snapshot is warmed first."""
        from kiro_crew.dashboard import ws as dashboard_ws
        from kiro_crew.dashboard.handlers import source_providers

        order: list[str] = []
        state = MagicMock()
        state.owner_id = "U_OWNER"
        state._yolo = False
        state.source_link_urls.return_value = []
        state.serialize_slots.side_effect = lambda **_kwargs: order.append("serialize") or []

        async def fake_ensure() -> frozenset:
            order.append("ensure")
            return frozenset()

        class Request(dict):
            def __init__(self) -> None:
                super().__init__({"user": "U_OWNER", "app": ""})
                # Mirror the auth middleware: it sets this POSITIVE flag so the
                # WS layer never infers trust from a falsy app claim.
                self.setdefault("is_dashboard_user", not self.get("app"))
                self.app = {"state": state}

        class FakeWebSocket:
            def __init__(self) -> None:
                self.closed = False
                self.sent: list[dict] = []
                # api_ws stores scope state on the socket via item assignment;
                # flag as a dashboard user so the WS scope gate passes through.
                self._flags: dict = {"_is_dashboard_user": True}

            def __setitem__(self, key: str, value) -> None:
                self._flags[key] = value

            def __getitem__(self, key: str):
                return self._flags[key]

            def get(self, key: str, default=None):
                return self._flags.get(key, default)

            async def prepare(self, request) -> None:
                return None

            async def send_json(self, payload: dict) -> None:
                self.sent.append(payload)

            async def send_str(self, payload: str) -> None:
                # Connect snapshot sends a pre-dumped string (offender-note
                # seam); parse back so assertions keep reading dict frames.
                self.sent.append(json.loads(payload))

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        fake_ws = FakeWebSocket()
        monkeypatch.setattr(dashboard_ws, "_check_ws_origin", lambda request: None)
        monkeypatch.setattr(dashboard_ws.web, "WebSocketResponse", lambda **kwargs: fake_ws)
        monkeypatch.setattr(source_providers, "ensure_gitlab_hosts_loaded", fake_ensure)
        monkeypatch.setattr(source_providers, "CHECK_STATUS_TTL_SECS", 30)

        await await_until(asyncio.ensure_future(dashboard_ws.api_ws(Request())))  # type: ignore[arg-type]

        assert order[:2] == ["ensure", "serialize"]

    @pytest.mark.asyncio
    async def test_app_token_ws_never_starts_refresh_loop(self, monkeypatch) -> None:
        """An app token renders no chip status (public or private), so it must
        not spawn the periodic provider driver. Only owner or dashboard-user
        connections do."""
        from kiro_crew.dashboard import ws as dashboard_ws
        from kiro_crew.dashboard import ws_event_scope
        from kiro_crew.dashboard.handlers import source_providers

        state = MagicMock()
        state.owner_id = "U_OWNER"
        state.serialize_slots.return_value = []
        state._yolo = False

        class Request(dict):
            def __init__(self) -> None:
                # An app token: non-empty app claim, NOT a dashboard user.
                super().__init__({"user": "U_OWNER", "app": "source-app"})
                self.setdefault("is_dashboard_user", not self.get("app"))
                self.app = {"state": state}

        refresh = MagicMock()

        class FakeWebSocket:
            def __init__(self) -> None:
                self.closed = False
                self.sent: list[dict] = []
                self._flags: dict = {"_is_dashboard_user": False}

            def __setitem__(self, key: str, value) -> None:
                self._flags[key] = value

            def __getitem__(self, key: str):
                return self._flags[key]

            def get(self, key: str, default=None):
                return self._flags.get(key, default)

            async def prepare(self, request) -> None:
                return None

            async def send_json(self, payload: dict) -> None:
                self.sent.append(payload)

            async def send_str(self, payload: str) -> None:
                # Connect snapshot sends a pre-dumped string (offender-note
                # seam); parse back so assertions keep reading dict frames.
                self.sent.append(json.loads(payload))

            def __aiter__(self):
                return self

            async def __anext__(self):
                # flake-ok: pre-existing 50 ms window for a wrongly started refresh loop to fire, moved verbatim; tracked for a de-flake pass
                # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 50 ms window before the negative assertion, moved verbatim; tracked for a de-flake pass
                await asyncio.sleep(0.05)
                raise StopAsyncIteration

        fake_ws = FakeWebSocket()
        monkeypatch.setattr(ws_event_scope, "is_app_enabled", lambda _name: True)
        monkeypatch.setattr(ws_event_scope, "get_app_manifest", lambda _name: None)
        monkeypatch.setattr(ws_event_scope, "_declared_cache", {})
        monkeypatch.setattr(dashboard_ws, "_check_ws_origin", lambda request: None)
        monkeypatch.setattr(dashboard_ws.web, "WebSocketResponse", lambda **kwargs: fake_ws)
        monkeypatch.setattr(source_providers, "CHECK_STATUS_TTL_SECS", 0.01)
        monkeypatch.setattr(source_providers, "schedule_check_refresh", refresh)
        monkeypatch.setattr(source_providers, "schedule_visibility_refresh", MagicMock())

        await await_until(asyncio.ensure_future(dashboard_ws.api_ws(Request())))  # type: ignore[arg-type]

        refresh.assert_not_called()
        state.source_link_urls.assert_not_called()
        assert fake_ws.sent[0]["type"] == "slots"

    @pytest.mark.asyncio
    async def test_non_owner_dashboard_user_does_not_start_refresh_loop(self, monkeypatch) -> None:
        """A signed-in dashboard user who is NOT the owner renders PUBLIC-repo
        chip status READ-ONLY from the owner-populated caches, so it must NOT
        start the periodic driver — both the status and visibility refreshes run
        the operator's credentials and are owner-only."""
        from kiro_crew.dashboard import ws as dashboard_ws
        from kiro_crew.dashboard.handlers import source_providers

        state = MagicMock()
        state.owner_id = "U_OWNER"
        state.serialize_slots.return_value = []
        state._yolo = False
        # A real list so the loop's modulo has a real length (no MagicMock len).
        state.source_link_urls.return_value = ["https://github.com/acme/repo/pull/1"]

        class Request(dict):
            def __init__(self) -> None:
                super().__init__({"user": "U_OTHER", "app": ""})
                self.setdefault("is_dashboard_user", not self.get("app"))
                self.app = {"state": state}

        check_refresh = MagicMock()
        vis_refresh = MagicMock()

        class FakeWebSocket:
            def __init__(self) -> None:
                self.closed = False
                self.sent: list[dict] = []
                self._flags: dict = {"_is_dashboard_user": True}

            def __setitem__(self, key: str, value) -> None:
                self._flags[key] = value

            def __getitem__(self, key: str):
                return self._flags[key]

            def get(self, key: str, default=None):
                return self._flags.get(key, default)

            async def prepare(self, request) -> None:
                return None

            async def send_json(self, payload: dict) -> None:
                self.sent.append(payload)

            async def send_str(self, payload: str) -> None:
                # Connect snapshot sends a pre-dumped string (offender-note
                # seam); parse back so assertions keep reading dict frames.
                self.sent.append(json.loads(payload))

            def __aiter__(self):
                return self

            async def __anext__(self):
                # flake-ok: pre-existing 50 ms window for a wrongly started refresh loop to fire, moved verbatim; tracked for a de-flake pass
                # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 50 ms window before the negative assertion, moved verbatim; tracked for a de-flake pass
                await asyncio.sleep(0.05)
                raise StopAsyncIteration

        fake_ws = FakeWebSocket()
        monkeypatch.setattr(dashboard_ws, "_check_ws_origin", lambda request: None)
        monkeypatch.setattr(dashboard_ws.web, "WebSocketResponse", lambda **kwargs: fake_ws)
        monkeypatch.setattr(source_providers, "CHECK_STATUS_TTL_SECS", 0.01)
        monkeypatch.setattr(source_providers, "schedule_check_refresh", check_refresh)
        monkeypatch.setattr(source_providers, "schedule_visibility_refresh", vis_refresh)

        await await_until(asyncio.ensure_future(dashboard_ws.api_ws(Request())))  # type: ignore[arg-type]
        # Non-owner: the driver never starts, so neither refresh is ever called
        # and source_link_urls is never polled by a refresh round.
        assert not check_refresh.called
        assert not vis_refresh.called

    @pytest.mark.asyncio
    async def test_refresh_loop_rotates_offset_across_rounds(self, monkeypatch) -> None:
        """Findings #1: with more stale chips than the per-round admission cap,
        the driver must rotate which URLs it submits first so every chip is
        eventually refreshed instead of the same slot-order prefix winning
        every TTL (deterministic starvation of newer slots)."""
        from kiro_crew.dashboard import ws as dashboard_ws
        from kiro_crew.dashboard.handlers import source_providers

        urls = [
            "https://github.com/acme/repo/pull/1",
            "https://github.com/acme/repo/pull/2",
            "https://github.com/acme/repo/pull/3",
        ]
        state = MagicMock()
        state.owner_id = "U_OWNER"
        state.serialize_slots.return_value = []
        state._yolo = False
        state.source_link_urls.return_value = list(urls)

        class Request(dict):
            def __init__(self) -> None:
                super().__init__({"user": "U_OWNER", "app": ""})
                # Mirror the auth middleware: it sets this POSITIVE flag so the
                # WS layer never infers trust from a falsy app claim.
                self.setdefault("is_dashboard_user", not self.get("app"))
                self.app = {"state": state}

        done = asyncio.Event()
        leads: list[str] = []

        def refresh(submitted, on_update=None):
            leads.append(submitted[0])
            if len(leads) >= 3:
                done.set()

        class FakeWebSocket:
            def __init__(self) -> None:
                self.closed = False
                self.sent: list[dict] = []
                # api_ws stores scope state on the socket via item assignment;
                # flag as a dashboard user so the WS scope gate passes through.
                self._flags: dict = {"_is_dashboard_user": True}

            def __setitem__(self, key: str, value) -> None:
                self._flags[key] = value

            def __getitem__(self, key: str):
                return self._flags[key]

            def get(self, key: str, default=None):
                return self._flags.get(key, default)

            async def prepare(self, request) -> None:
                return None

            async def send_json(self, payload: dict) -> None:
                self.sent.append(payload)

            async def send_str(self, payload: str) -> None:
                # Connect snapshot sends a pre-dumped string (offender-note
                # seam); parse back so assertions keep reading dict frames.
                self.sent.append(json.loads(payload))

            def __aiter__(self):
                return self

            async def __anext__(self):
                # flake-ok: pre-existing unbounded wait that holds the fake socket open, moved verbatim; the await_until around api_ws bounds the test
                await done.wait()
                raise StopAsyncIteration

        fake_ws = FakeWebSocket()
        monkeypatch.setattr(dashboard_ws, "_check_ws_origin", lambda request: None)
        monkeypatch.setattr(dashboard_ws.web, "WebSocketResponse", lambda **kwargs: fake_ws)
        monkeypatch.setattr(source_providers, "CHECK_STATUS_TTL_SECS", 0.01)
        monkeypatch.setattr(source_providers, "CHECK_STATUS_PENDING_MAX", 2)
        monkeypatch.setattr(source_providers, "schedule_check_refresh", refresh)

        await await_until(asyncio.ensure_future(dashboard_ws.api_ws(Request())))  # type: ignore[arg-type]

        # offset = round * cap(2) % len(3): rounds 0,1,2 lead with index 0,2,1 —
        # every URL leads within ceil(len/cap) rounds, so none is starved.
        assert leads[:3] == [urls[0], urls[2], urls[1]]
        assert set(leads[:3]) == set(urls)

    @pytest.mark.asyncio
    async def test_refresh_loop_survives_transient_exception(self, monkeypatch) -> None:
        """Findings #2: a single transient failure inside a refresh round must
        be logged and swallowed so the driver keeps running, rather than
        silently dying and reverting to the frozen-chip bug it fixes."""
        from kiro_crew.dashboard import ws as dashboard_ws
        from kiro_crew.dashboard.handlers import source_providers

        url = "https://github.com/acme/repo/pull/248"
        state = MagicMock()
        state.owner_id = "U_OWNER"
        state.serialize_slots.return_value = []
        state._yolo = False
        state.source_link_urls.return_value = [url]

        class Request(dict):
            def __init__(self) -> None:
                super().__init__({"user": "U_OWNER", "app": ""})
                # Mirror the auth middleware: it sets this POSITIVE flag so the
                # WS layer never infers trust from a falsy app claim.
                self.setdefault("is_dashboard_user", not self.get("app"))
                self.app = {"state": state}

        recovered = asyncio.Event()
        calls: list[str] = []

        def refresh(submitted, on_update=None):
            calls.append(submitted[0])
            if len(calls) == 1:
                raise RuntimeError("transient provider glitch")
            recovered.set()

        class FakeWebSocket:
            def __init__(self) -> None:
                self.closed = False
                self.sent: list[dict] = []
                # api_ws stores scope state on the socket via item assignment;
                # flag as a dashboard user so the WS scope gate passes through.
                self._flags: dict = {"_is_dashboard_user": True}

            def __setitem__(self, key: str, value) -> None:
                self._flags[key] = value

            def __getitem__(self, key: str):
                return self._flags[key]

            def get(self, key: str, default=None):
                return self._flags.get(key, default)

            async def prepare(self, request) -> None:
                return None

            async def send_json(self, payload: dict) -> None:
                self.sent.append(payload)

            async def send_str(self, payload: str) -> None:
                # Connect snapshot sends a pre-dumped string (offender-note
                # seam); parse back so assertions keep reading dict frames.
                self.sent.append(json.loads(payload))

            def __aiter__(self):
                return self

            async def __anext__(self):
                # flake-ok: pre-existing unbounded wait that holds the fake socket open, moved verbatim; the await_until around api_ws bounds the test
                await recovered.wait()
                raise StopAsyncIteration

        fake_ws = FakeWebSocket()
        monkeypatch.setattr(dashboard_ws, "_check_ws_origin", lambda request: None)
        monkeypatch.setattr(dashboard_ws.web, "WebSocketResponse", lambda **kwargs: fake_ws)
        monkeypatch.setattr(source_providers, "CHECK_STATUS_TTL_SECS", 0.01)
        monkeypatch.setattr(source_providers, "schedule_check_refresh", refresh)

        await await_until(asyncio.ensure_future(dashboard_ws.api_ws(Request())))  # type: ignore[arg-type]

        # Fired at least twice: the first raised, the loop logged and continued.
        assert len(calls) >= 2


class TestTurnBoundarySourceStatus:
    """Regression: PR state must not lag the session that just changed it.

    Before this, nothing invalidated either status cache when an agent turn
    ended — the chips waited out the periodic rotation (minutes, with more
    PR-linked slots than the per-round admission cap) and the detail panel never
    refetched at all, so the sidebar and the panel could show different
    lifecycles for the same PR indefinitely.
    """

    def test_per_slot_urls_are_scoped_and_capped(self, state: DashboardState) -> None:
        slot = state.get_or_create_slot("chat-a")
        for n in (1, 2, 3, 4):
            slot.append("assistant", f"see https://github.com/acme/repo/pull/{n}", broadcast=False)
        other = state.get_or_create_slot("chat-b")
        other.append("assistant", "and https://github.com/acme/other/pull/9", broadcast=False)

        # Only this slot's chips, capped at the serialized count — a turn ending
        # in one session must not fan provider reads across every other session.
        assert state.source_link_urls_for_slot("chat-a") == [
            "https://github.com/acme/repo/pull/4",
            "https://github.com/acme/repo/pull/3",
            "https://github.com/acme/repo/pull/2",
        ]
        assert state.source_link_urls_for_slot("nope") == []

    def test_turn_boundary_forces_refresh_for_owner(
        self, state: DashboardState, monkeypatch
    ) -> None:
        from kiro_crew.dashboard.handlers import source_providers

        slot = state.get_or_create_slot("chat-a")
        slot.append("assistant", "opened https://github.com/acme/repo/pull/7", broadcast=False)
        state._owner_ws_clients.add(MagicMock(closed=False))
        calls: list[tuple] = []
        monkeypatch.setattr(
            source_providers,
            "request_check_refresh_now",
            lambda urls, on_update=None: calls.append((urls, on_update)),
        )

        state.refresh_slot_source_status("chat-a")

        assert calls == [(["https://github.com/acme/repo/pull/7"], state.push_slots_update)]

    def test_turn_boundary_is_a_noop_without_an_owner_window(
        self, state: DashboardState, monkeypatch
    ) -> None:
        """Status is credential-backed and only owners render it, so a headless
        or non-owner gateway must not spawn provider subprocesses per turn."""
        from kiro_crew.dashboard.handlers import source_providers

        slot = state.get_or_create_slot("chat-a")
        slot.append("assistant", "opened https://github.com/acme/repo/pull/7", broadcast=False)
        state._owner_ws_clients.clear()
        refresh = MagicMock()
        monkeypatch.setattr(source_providers, "request_check_refresh_now", refresh)

        state.refresh_slot_source_status("chat-a")

        refresh.assert_not_called()
        state._owner_ws_clients.add(MagicMock(closed=False))
        state.refresh_slot_source_status("chat-a")
        assert refresh.call_count == 1
        assert refresh.call_args.args[0] == ["https://github.com/acme/repo/pull/7"]

    def test_turn_boundary_non_owner_drives_no_refresh(
        self, state: DashboardState, monkeypatch
    ) -> None:
        """At a turn boundary with ONLY a non-owner
        dashboard-user window open (no owner window), NEITHER the credentialed
        status read NOR the visibility probe fires — both run the operator's
        credentials and are owner-only. A non-owner audience triggers nothing."""
        from kiro_crew.dashboard import state as state_mod  # noqa: F401
        from kiro_crew.dashboard.handlers import source_providers

        slot = state.get_or_create_slot("chat-a")
        slot.append(
            "assistant",
            "pub https://github.com/acme/pub/pull/1 priv https://github.com/acme/priv/pull/2",
            broadcast=False,
        )
        # No owner window; one non-owner dashboard-user window.
        state._owner_ws_clients.clear()
        _du_ws = MagicMock(closed=False)
        _du_ws.get.side_effect = lambda k, d=None: True if k == "_is_dashboard_user" else d
        state._ws_clients = [_du_ws]
        status_calls: list[tuple] = []
        vis_calls: list[tuple] = []
        monkeypatch.setattr(
            source_providers,
            "request_check_refresh_now",
            lambda urls, on_update=None: status_calls.append((list(urls), on_update)),
        )
        monkeypatch.setattr(
            source_providers,
            "schedule_visibility_refresh",
            lambda urls, on_update=None, *, force=False: vis_calls.append((list(urls), force)),
        )

        state.refresh_slot_source_status("chat-a")

        # No owner window → the turn-boundary refresh is a no-op: neither the
        # status read nor the visibility probe runs.
        assert status_calls == []
        assert vis_calls == []

    def test_turn_boundary_swallows_refresh_failures(
        self, state: DashboardState, monkeypatch
    ) -> None:
        """A status refresh is best-effort telemetry; it must never be able to
        break the turn-completion path it hangs off."""
        from kiro_crew.dashboard.handlers import source_providers

        slot = state.get_or_create_slot("chat-a")
        slot.append("assistant", "opened https://github.com/acme/repo/pull/7", broadcast=False)
        state._owner_ws_clients.add(MagicMock(closed=False))
        refresh = MagicMock(side_effect=RuntimeError("no event loop"))
        monkeypatch.setattr(source_providers, "request_check_refresh_now", refresh)

        state.refresh_slot_source_status("chat-a")

        assert refresh.call_args.args[0] == ["https://github.com/acme/repo/pull/7"]

    def test_status_delta_goes_only_to_owner_sockets(
        self, state: DashboardState, monkeypatch
    ) -> None:
        sent: list[str] = []
        monkeypatch.setattr(state, "_send_ws_owners", lambda msg: sent.append(msg))

        # No owner connected → nothing is serialized or sent at all.
        state._owner_ws_clients.clear()
        state.push_source_status({"url": "https://github.com/acme/repo/pull/7", "state": "merged"})
        assert sent == []

        state._owner_ws_clients.add(MagicMock(closed=False))
        state.push_source_status({"url": "https://github.com/acme/repo/pull/7", "state": "merged"})

        assert json.loads(sent[0]) == {
            "type": "source_status",
            "data": {"url": "https://github.com/acme/repo/pull/7", "state": "merged"},
        }

    @pytest.mark.asyncio
    async def test_wire_status_delta_sink_registers_and_cleans_up(
        self, state: DashboardState
    ) -> None:
        """The dashboard wiring must register the owner-scoped sink AND clean it up.

        This closes a production-wiring gap: the transport tests above call
        ``push_source_status`` / ``register_status_delta_sink`` directly, so they
        would stay green even if ``start_dashboard`` stopped wiring the sink or
        dropped its shutdown cleanup. This drives the real wiring helper: it must
        register exactly the state's ``push_source_status`` and, on app shutdown,
        unregister it (the sink set is module-global and would otherwise leak
        dead states across dashboard restarts, double-dispatching every delta).
        """
        from aiohttp import web

        from kiro_crew.dashboard import server
        from kiro_crew.dashboard.handlers import source_providers

        source_providers._status_delta_sinks.clear()
        app = web.Application()
        server._wire_status_delta_sink(app, state)

        assert state.push_source_status in source_providers._status_delta_sinks

        # Running the app's cleanup handlers must remove the sink.
        for cleanup in app.on_cleanup:
            await cleanup(app)
        assert state.push_source_status not in source_providers._status_delta_sinks
