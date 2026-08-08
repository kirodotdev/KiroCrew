"""Tests for DashboardState WebSocket subscriber methods (activity viewer)."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.dashboard.state import DashboardState


@pytest.fixture(autouse=True)
def sync_event_loop():
    """Provide an event loop for sync tests calling asyncio.ensure_future.

    Production broadcast methods use ensure_future (fire-and-forget) which
    requires a running event loop.  Under xdist each worker is a separate
    process with no default loop, so we create one here.  autouse=True
    ensures every test gets a loop without opt-in, preventing flakes when
    new broadcast tests are added.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()
    asyncio.set_event_loop(None)


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    return DashboardState(
        sessions=MagicMock(count=0),
        crons=MagicMock(),
        lessons=MagicMock(),
        start_time=0.0,
    )


class TestSubagentSubscribers:
    def test_subscribe_and_unsubscribe(self, state: DashboardState) -> None:
        ws = MagicMock()
        state.subscribe_subagents(ws)
        assert ws in state._ws_subagent_subscribers
        state.unsubscribe_subagents(ws)
        assert ws not in state._ws_subagent_subscribers

    def test_unsubscribe_idempotent(self, state: DashboardState) -> None:
        ws = MagicMock()
        state.unsubscribe_subagents(ws)  # should not raise

    def test_broadcast_sends_to_subscribed_only(self, state: DashboardState) -> None:
        ws_sub = MagicMock(closed=False)
        ws_sub.send_str = AsyncMock()
        ws_nosub = MagicMock(closed=False)
        ws_nosub.send_str = AsyncMock()
        state.subscribe_subagents(ws_sub)
        state.register_ws(ws_nosub)
        state.broadcast_ws_subagent_subscribers("subagent_chunk", {"id": "a1", "text": "hi"})
        ws_sub.send_str.assert_called_once()
        payload = json.loads(ws_sub.send_str.call_args[0][0])
        assert payload["type"] == "subagent_chunk"
        assert payload["data"]["id"] == "a1"
        ws_nosub.send_str.assert_not_called()

    def test_broadcast_noop_when_empty(self, state: DashboardState) -> None:
        state.broadcast_ws_subagent_subscribers("subagent_chunk", {"id": "a1"})

    def test_broadcast_ws_sends_to_all(self, state: DashboardState) -> None:
        ws1 = MagicMock(closed=False)
        ws1.send_str = AsyncMock()
        ws2 = MagicMock(closed=False)
        ws2.send_str = AsyncMock()
        state.register_ws(ws1)
        state.register_ws(ws2)
        state.broadcast_ws("subagent_spawn", {"id": "a1", "slot": "chat-1"})
        ws1.send_str.assert_called_once()
        ws2.send_str.assert_called_once()

    def test_broken_subscriber_removed(self, state: DashboardState) -> None:
        ws = MagicMock(closed=False)
        ws.send_str = MagicMock(side_effect=ConnectionResetError)
        state.subscribe_subagents(ws)
        state.broadcast_ws_subagent_subscribers("subagent_chunk", {"id": "a1"})
        assert ws not in state._ws_subagent_subscribers

    def test_closed_ws_removed_on_broadcast(self, state: DashboardState) -> None:
        ws_alive = MagicMock(closed=False)
        ws_alive.send_str = AsyncMock()
        ws_dead = MagicMock(closed=True)
        ws_dead.send_str = AsyncMock()
        state.register_ws(ws_alive)
        state.register_ws(ws_dead)
        state.broadcast_ws("test", {"x": 1})
        ws_alive.send_str.assert_called_once()
        ws_dead.send_str.assert_not_called()
        assert ws_dead not in state._ws_clients
        assert ws_alive in state._ws_clients


class TestOwnerScopedBroadcast:
    """Owner-only typed broadcast + its delivery count (PR #461)."""

    @staticmethod
    def _ws(closed: bool = False) -> MagicMock:
        ws = MagicMock()
        ws.closed = closed
        ws.send_str = AsyncMock()
        return ws

    @pytest.mark.asyncio
    async def test_only_owner_clients_receive_the_message(self, state: DashboardState) -> None:
        owner, other = self._ws(), self._ws()
        state.register_ws(owner, owner=True)
        state.register_ws(other)
        await state.deliver_ws_owners("followup_card", {"slot": "chat-1"})
        assert owner.send_str.await_count or owner.send_str.call_count
        assert not (other.send_str.await_count or other.send_str.call_count)

    def test_count_excludes_non_owner_clients(self, state: DashboardState) -> None:
        state.register_ws(self._ws())
        state.register_ws(self._ws())
        assert state.ws_client_count() == 2

    @pytest.mark.asyncio
    async def test_awaited_delivery_counts_only_completed_sends(
        self, state: DashboardState
    ) -> None:
        """Round 12 BLOCKING: a socket count is taken BEFORE any send runs, so a
        peer that drops in that window was reported as delivered. Only a send
        that completed counts."""
        good, broken = self._ws(), self._ws()
        broken.send_str = AsyncMock(side_effect=ConnectionResetError("peer gone"))
        state.register_ws(good, owner=True)
        state.register_ws(broken, owner=True)
        delivered = await state.deliver_ws_owners("followup_card", {"slot": "chat-1"})
        assert delivered == 1
        assert broken not in state._owner_ws_clients
        assert good in state._owner_ws_clients

    @pytest.mark.asyncio
    async def test_awaited_delivery_excludes_non_owner_and_closed(
        self, state: DashboardState
    ) -> None:
        """A closed socket receives nothing, and an app token in `_ws_clients`
        must never be counted as reach for owner-scoped content."""
        state.register_ws(self._ws(), owner=True)
        state.register_ws(self._ws(closed=True), owner=True)
        other = self._ws()
        state.register_ws(other)
        assert await state.deliver_ws_owners("followup_card", {"slot": "chat-1"}) == 1
        assert not (other.send_str.await_count or other.send_str.call_count)

    @pytest.mark.asyncio
    async def test_awaited_delivery_with_no_owner_clients_is_zero(
        self, state: DashboardState
    ) -> None:
        state.register_ws(self._ws())
        assert await state.deliver_ws_owners("followup_card", {"slot": "chat-1"}) == 0

    @pytest.mark.asyncio
    async def test_no_owner_clients_is_a_noop(self, state: DashboardState) -> None:
        other = self._ws()
        state.register_ws(other)
        assert await state.deliver_ws_owners("followup_card", {"slot": "chat-1"}) == 0
        assert not (other.send_str.await_count or other.send_str.call_count)


class TestSlotModel:
    def test_model_in_to_dict(self, state: DashboardState) -> None:
        slot = state.get_or_create_slot("test-1", model="claude-opus-4.5")
        assert slot.to_dict()["model"] == "claude-opus-4.5"

    def test_model_defaults_empty(self, state: DashboardState) -> None:
        slot = state.get_or_create_slot("test-2")
        assert slot.model == ""


class TestChatSlotStopState:
    """Tests for _ChatSlot._stop_state and _stopping property."""

    def test_stop_state_default_idle(self) -> None:
        from kiro_crew.dashboard.state import _ChatSlot

        slot = _ChatSlot("s1")
        assert slot._stop_state == "idle"
        assert slot._stopping is False
        assert slot._native_subagent_tracker == {}
        assert slot._native_subagent_output == {}

    def test_stopping_property_reflects_stop_state(self) -> None:
        from kiro_crew.dashboard.state import _ChatSlot

        slot = _ChatSlot("s1")
        slot._stop_state = "soft_pending"
        assert slot._stopping is True
        slot._stop_state = "killing"
        assert slot._stopping is True
        slot._stop_state = "idle"
        assert slot._stopping is False

    def test_stopping_setter_compat(self) -> None:
        from kiro_crew.dashboard.state import _ChatSlot

        slot = _ChatSlot("s1")
        slot._stopping = True
        assert slot._stop_state == "soft_pending"
        slot._stopping = False
        assert slot._stop_state == "idle"

    def test_to_dict_includes_stop_state(self) -> None:
        from kiro_crew.dashboard.state import _ChatSlot

        slot = _ChatSlot("s1")
        d = slot.to_dict()
        assert d["stop_state"] == "idle"
        slot._stop_state = "soft_pending"
        d = slot.to_dict()
        assert d["stop_state"] == "soft_pending"
        assert d["stopping"] is True


class TestCompactCallbackWiring:
    """Tests for DashboardState.wire_session_compact_callback.

    Covers the async closure that fires after SessionManager recycles a
    dashboard session: posts a visible notice and broadcasts context_usage
    reset.  Non-dashboard session keys and missing slots short-circuit.
    """

    def _captured_callback(self, state: DashboardState):
        """Install the callback and return the closure passed to sessions."""
        state.wire_session_compact_callback()
        state.sessions.set_compact_callback.assert_called_once()
        return state.sessions.set_compact_callback.call_args[0][0]

    def test_wire_installs_callback_on_sessions(self, state: DashboardState) -> None:
        state.wire_session_compact_callback()
        state.sessions.set_compact_callback.assert_called_once()
        cb = state.sessions.set_compact_callback.call_args[0][0]
        assert callable(cb)

    @pytest.mark.asyncio
    async def test_callback_ignores_non_dashboard_keys(self, state: DashboardState) -> None:
        slot = state.get_or_create_slot("chat-1")
        baseline = len(slot.messages)
        cb = self._captured_callback(state)

        await cb("heartbeat", 90.0, success=True)
        await cb("cron:daily-digest", 95.0, success=True)

        assert len(slot.messages) == baseline

    @pytest.mark.asyncio
    async def test_callback_routes_channel_keys_to_channel_notice(
        self, state: DashboardState
    ) -> None:
        """A Slack/Discord session has no slot, so the notice goes to its channel."""
        slot = state.get_or_create_slot("chat-1")
        baseline = len(slot.messages)
        cb = self._captured_callback(state)

        with patch(
            "kiro_crew.dashboard.state.deliver_channel_compaction_notice",
            new_callable=AsyncMock,
        ) as deliver:
            await cb("slack:1785370133.085469", 92.0, success=True)
            await cb("discord:kirocrew:direct:u1", 93.0, success=False)

        assert [c.args[1] for c in deliver.await_args_list] == [
            "slack:1785370133.085469",
            "discord:kirocrew:direct:u1",
        ]
        assert deliver.await_args_list[1].kwargs["success"] is False
        # The channel leg must not also write into an unrelated dashboard slot.
        assert len(slot.messages) == baseline

    @pytest.mark.asyncio
    async def test_channel_notice_failure_does_not_propagate(
        self, state: DashboardState
    ) -> None:
        """The compaction already succeeded; a broken channel must not raise."""
        cb = self._captured_callback(state)

        with patch(
            "kiro_crew.dashboard.state.deliver_channel_compaction_notice",
            new_callable=AsyncMock,
            side_effect=RuntimeError("transport exploded"),
        ):
            await cb("slack:1785370133.085469", 92.0, success=True)

    @pytest.mark.asyncio
    async def test_callback_noop_when_slot_missing(self, state: DashboardState) -> None:
        cb = self._captured_callback(state)

        # No slot named chat-ghost exists.  Must not raise.
        await cb("dashboard:chat-ghost", 90.0, success=True)

    @pytest.mark.asyncio
    async def test_callback_appends_assistant_notice(self, state: DashboardState) -> None:
        slot = state.get_or_create_slot("chat-1")
        before = len(slot.messages)
        cb = self._captured_callback(state)

        await cb("dashboard:chat-1", 92.0, success=True)

        assert len(slot.messages) == before + 1
        added = slot.messages[-1]
        assert added["role"] == "assistant"
        assert added["cls"] == "msg msg-a"
        assert "92" in added["content"]
        assert "Auto-compacted" in added["content"]
        # Tagged kind="compaction" so the proactive notice does not shadow the
        # follow-up [OPTIONS:] backward scan (deriveFollowUpOptions).
        assert added.get("meta", {}).get("kind") == "compaction"

    @pytest.mark.asyncio
    async def test_callback_rounds_pct_in_notice(self, state: DashboardState) -> None:
        """`{pct:.0f}` format keeps the notice terse — 91.7 renders as 92."""
        state.get_or_create_slot("chat-1")
        cb = self._captured_callback(state)

        await cb("dashboard:chat-1", 91.7, success=True)

        added = state.get_slot("chat-1").messages[-1]
        assert "92%" in added["content"]

    @pytest.mark.asyncio
    async def test_callback_broadcasts_context_usage_reset(self, state: DashboardState) -> None:
        ws = MagicMock(closed=False)
        ws.send_str = AsyncMock()
        state.register_ws(ws)
        state.get_or_create_slot("chat-1")
        cb = self._captured_callback(state)

        await cb("dashboard:chat-1", 92.0, success=True)

        payloads = [json.loads(c.args[0]) for c in ws.send_str.call_args_list]
        context = [p for p in payloads if p.get("type") == "context_usage"]
        assert len(context) == 1
        # reset lets the frontend drop its stored token counts too — they
        # describe the pre-compaction transcript.
        assert context[0]["data"] == {"slot": "chat-1", "pct": 0.0, "reset": True}

    @pytest.mark.asyncio
    async def test_callback_broadcast_runs_even_if_append_fails(
        self, state: DashboardState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.dashboard.state import _ChatSlot

        ws = MagicMock(closed=False)
        ws.send_str = AsyncMock()
        state.register_ws(ws)
        state.get_or_create_slot("chat-1")
        # _ChatSlot uses __slots__, so monkeypatch at the class level.
        monkeypatch.setattr(_ChatSlot, "append", MagicMock(side_effect=RuntimeError("append boom")))
        cb = self._captured_callback(state)

        await cb("dashboard:chat-1", 92.0, success=True)

        payloads = [json.loads(c.args[0]) for c in ws.send_str.call_args_list]
        context = [p for p in payloads if p.get("type") == "context_usage"]
        assert len(context) == 1

    @pytest.mark.asyncio
    async def test_callback_broadcast_failure_does_not_propagate(
        self, state: DashboardState
    ) -> None:
        slot = state.get_or_create_slot("chat-1")
        cb = self._captured_callback(state)
        # Force broadcast to raise — append should still land, callback should return cleanly
        with pytest.MonkeyPatch.context() as mp:

            def boom(*a, **kw):
                raise RuntimeError("ws boom")

            mp.setattr(state, "broadcast_ws", boom)

            await cb("dashboard:chat-1", 92.0, success=True)

        assert slot.messages[-1]["role"] == "assistant"


def test_folder_breadcrumb_walks_full_ancestry(state):
    state._folders = [
        {"id": "a", "name": "KiroCrew", "parent_id": ""},
        {"id": "b", "name": "Backend", "parent_id": "a"},
        {"id": "c", "name": "auth-refactor", "parent_id": "b"},
    ]
    assert state.folder_breadcrumb("c") == "KiroCrew › Backend › auth-refactor"


def test_folder_breadcrumb_single_root(state):
    state._folders = [{"id": "a", "name": "KiroCrew", "parent_id": ""}]
    assert state.folder_breadcrumb("a") == "KiroCrew"


def test_folder_breadcrumb_empty_or_unknown_id(state):
    state._folders = [{"id": "a", "name": "KiroCrew", "parent_id": ""}]
    assert state.folder_breadcrumb("") == ""
    assert state.folder_breadcrumb("missing") == ""


def test_folder_breadcrumb_dangling_parent(state):
    # parent_id points at a folder that no longer exists — walk stops gracefully.
    state._folders = [{"id": "b", "name": "Backend", "parent_id": "gone"}]
    assert state.folder_breadcrumb("b") == "Backend"


def test_folder_breadcrumb_cycle_safe(state):
    state._folders = [
        {"id": "a", "name": "A", "parent_id": "b"},
        {"id": "b", "name": "B", "parent_id": "a"},
    ]
    # No infinite loop; each visited once.
    assert state.folder_breadcrumb("a") == "B › A"


class TestOwnerSourceStatusTransport:
    def test_slot_updates_keep_status_out_of_sse_and_generic_websockets(
        self, state: DashboardState, monkeypatch
    ) -> None:
        source_url = "https://github.com/acme/repo/pull/12"

        def serialize_slots(*, include_check_status: bool = False) -> list[dict]:
            link = {"url": source_url, "provider": "github", "number": 12}
            if include_check_status:
                link.update({"ci": "passed", "state": "OPEN"})
            return [{"key": "chat-1", "source_links": [link]}]

        monkeypatch.setattr(state, "serialize_slots", serialize_slots)
        monkeypatch.setattr(state, "is_yolo_active", lambda: False)
        sent: list[tuple[object, dict]] = []
        monkeypatch.setattr(
            state,
            "_spawn_ws_send",
            lambda client, message: sent.append((client, json.loads(message))),
        )
        generic_ws = MagicMock(closed=False)
        owner_ws = MagicMock(closed=False)
        state.register_ws(generic_ws)
        state.register_ws(owner_ws, owner=True)
        sse_queue = state.register_sse()

        state.push_slots_update()

        sse_note = sse_queue.get_nowait()
        assert "ci" not in str(sse_note["_slots_list"])
        assert "state" not in sse_note["_slots_list"][0]["source_links"][0]

        generic_messages = [message for client, message in sent if client is generic_ws]
        owner_messages = [message for client, message in sent if client is owner_ws]
        assert len(generic_messages) == 1
        assert "ci" not in str(generic_messages[0]["data"])
        assert "state" not in generic_messages[0]["data"][0]["source_links"][0]
        assert len(owner_messages) == 2
        assert "ci" not in str(owner_messages[0]["data"])
        assert owner_messages[1]["data"][0]["source_links"][0]["ci"] == "passed"
        assert owner_messages[1]["data"][0]["source_links"][0]["state"] == "OPEN"

    @pytest.mark.parametrize(
        ("claims", "owner_request"),
        [
            ({"user": "U_OWNER", "app": ""}, True),
            ({"user": "U_OTHER", "app": ""}, False),
            ({"user": "U_OWNER", "app": "source-app"}, False),
        ],
    )
    @pytest.mark.asyncio
    async def test_websocket_initial_status_and_refresh_are_owner_only(
        self, monkeypatch, claims, owner_request
    ) -> None:
        from kiro_crew.dashboard import ws as dashboard_ws
        from kiro_crew.dashboard.handlers import source_providers

        source_url = "https://github.com/acme/repo/pull/12"

        def serialize_slots(*, include_check_status: bool = False) -> list[dict]:
            link = {"url": source_url, "provider": "github", "number": 12}
            if include_check_status:
                link.update({"ci": "passed", "state": "OPEN"})
            return [{"key": "chat-1", "source_links": [link]}]

        state = MagicMock()
        state.owner_id = "U_OWNER"
        state.serialize_slots.side_effect = serialize_slots
        state._yolo = False

        class Request(dict):
            def __init__(self) -> None:
                super().__init__(claims)
                self.app = {"state": state}

        class FakeWebSocket:
            def __init__(self) -> None:
                self.closed = True
                self.sent: list[dict] = []

            async def prepare(self, request) -> None:
                return None

            async def send_json(self, payload: dict) -> None:
                self.sent.append(payload)

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        fake_ws = FakeWebSocket()
        refresh = MagicMock()
        monkeypatch.setattr(dashboard_ws, "_check_ws_origin", lambda request: None)
        monkeypatch.setattr(dashboard_ws.web, "WebSocketResponse", lambda **kwargs: fake_ws)
        monkeypatch.setattr(source_providers, "schedule_check_refresh", refresh)

        result = await dashboard_ws.api_ws(Request())  # type: ignore[arg-type]
        await asyncio.sleep(0)

        assert result is fake_ws
        state.register_ws.assert_called_once_with(fake_ws, owner=owner_request)
        initial_slots = fake_ws.sent[0]["data"]
        if owner_request:
            assert initial_slots[0]["source_links"][0]["ci"] == "passed"
            refresh.assert_called_once_with([source_url], state.push_slots_update)
        else:
            assert "ci" not in str(initial_slots)
            assert "state" not in initial_slots[0]["source_links"][0]
            refresh.assert_not_called()
        state.unregister_ws.assert_called_once_with(fake_ws)


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
            slot_a.append("assistant", f"see https://github.com/acme/repo/pull/{n}", broadcast=False)
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

            async def prepare(self, request) -> None:
                return None

            async def send_json(self, payload: dict) -> None:
                self.sent.append(payload)

            def __aiter__(self):
                return self

            async def __anext__(self):
                # Hold the connection open until the periodic loop has fired
                # once, then end the handler (which cancels the loop task).
                await refreshed.wait()
                raise StopAsyncIteration

        fake_ws = FakeWebSocket()
        monkeypatch.setattr(dashboard_ws, "_check_ws_origin", lambda request: None)
        monkeypatch.setattr(dashboard_ws.web, "WebSocketResponse", lambda **kwargs: fake_ws)
        monkeypatch.setattr(source_providers, "CHECK_STATUS_TTL_SECS", 0.01)
        monkeypatch.setattr(source_providers, "schedule_check_refresh", refresh)

        await asyncio.wait_for(dashboard_ws.api_ws(Request()), timeout=5)  # type: ignore[arg-type]

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
                source_providers._publish_gitlab_hosts(frozenset({"gitlab.acme.internal"}))
            return frozenset()

        class Request(dict):
            def __init__(self) -> None:
                super().__init__({"user": "U_OWNER", "app": ""})
                self.app = {"state": state}

        class FakeWebSocket:
            def __init__(self) -> None:
                self.closed = False
                self.sent: list[dict] = []

            async def prepare(self, request) -> None:
                return None

            async def send_json(self, payload: dict) -> None:
                self.sent.append(payload)

            def __aiter__(self):
                return self

            async def __anext__(self):
                await pushed.wait()
                raise StopAsyncIteration

        monkeypatch.setattr(source_providers, "_gitlab_hosts_snapshot", frozenset())
        monkeypatch.setattr(source_providers, "_gitlab_hosts_loaded_at", 0.0)
        monkeypatch.setattr(source_providers, "_gitlab_hosts_generation", 0)
        monkeypatch.setattr(dashboard_ws, "_check_ws_origin", lambda request: None)
        monkeypatch.setattr(
            dashboard_ws.web, "WebSocketResponse", lambda **kwargs: FakeWebSocket()
        )
        monkeypatch.setattr(source_providers, "ensure_gitlab_hosts_loaded", fake_ensure)
        monkeypatch.setattr(source_providers, "CHECK_STATUS_TTL_SECS", 0.01)

        await asyncio.wait_for(dashboard_ws.api_ws(Request()), timeout=5)  # type: ignore[arg-type]

        state.push_slots_update.assert_called()

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
    async def test_ws_warms_gitlab_allowlist_before_first_serialization(
        self, monkeypatch
    ) -> None:
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
                self.app = {"state": state}

        class FakeWebSocket:
            def __init__(self) -> None:
                self.closed = False
                self.sent: list[dict] = []

            async def prepare(self, request) -> None:
                return None

            async def send_json(self, payload: dict) -> None:
                self.sent.append(payload)

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        fake_ws = FakeWebSocket()
        monkeypatch.setattr(dashboard_ws, "_check_ws_origin", lambda request: None)
        monkeypatch.setattr(dashboard_ws.web, "WebSocketResponse", lambda **kwargs: fake_ws)
        monkeypatch.setattr(source_providers, "ensure_gitlab_hosts_loaded", fake_ensure)
        monkeypatch.setattr(source_providers, "CHECK_STATUS_TTL_SECS", 30)

        await asyncio.wait_for(dashboard_ws.api_ws(Request()), timeout=5)  # type: ignore[arg-type]

        assert order[:2] == ["ensure", "serialize"]

    @pytest.mark.asyncio
    async def test_non_owner_ws_never_starts_refresh_loop(self, monkeypatch) -> None:
        from kiro_crew.dashboard import ws as dashboard_ws
        from kiro_crew.dashboard.handlers import source_providers

        state = MagicMock()
        state.owner_id = "U_OWNER"
        state.serialize_slots.return_value = []
        state._yolo = False

        class Request(dict):
            def __init__(self) -> None:
                super().__init__({"user": "U_OTHER", "app": ""})
                self.app = {"state": state}

        refresh = MagicMock()

        class FakeWebSocket:
            def __init__(self) -> None:
                self.closed = False
                self.sent: list[dict] = []

            async def prepare(self, request) -> None:
                return None

            async def send_json(self, payload: dict) -> None:
                self.sent.append(payload)

            def __aiter__(self):
                return self

            async def __anext__(self):
                # Stay open long enough for several 0.01s TTL ticks to elapse.
                await asyncio.sleep(0.05)
                raise StopAsyncIteration

        fake_ws = FakeWebSocket()
        monkeypatch.setattr(dashboard_ws, "_check_ws_origin", lambda request: None)
        monkeypatch.setattr(dashboard_ws.web, "WebSocketResponse", lambda **kwargs: fake_ws)
        monkeypatch.setattr(source_providers, "CHECK_STATUS_TTL_SECS", 0.01)
        monkeypatch.setattr(source_providers, "schedule_check_refresh", refresh)

        await asyncio.wait_for(dashboard_ws.api_ws(Request()), timeout=5)  # type: ignore[arg-type]

        refresh.assert_not_called()
        state.source_link_urls.assert_not_called()

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

            async def prepare(self, request) -> None:
                return None

            async def send_json(self, payload: dict) -> None:
                self.sent.append(payload)

            def __aiter__(self):
                return self

            async def __anext__(self):
                await done.wait()
                raise StopAsyncIteration

        fake_ws = FakeWebSocket()
        monkeypatch.setattr(dashboard_ws, "_check_ws_origin", lambda request: None)
        monkeypatch.setattr(dashboard_ws.web, "WebSocketResponse", lambda **kwargs: fake_ws)
        monkeypatch.setattr(source_providers, "CHECK_STATUS_TTL_SECS", 0.01)
        monkeypatch.setattr(source_providers, "CHECK_STATUS_PENDING_MAX", 2)
        monkeypatch.setattr(source_providers, "schedule_check_refresh", refresh)

        await asyncio.wait_for(dashboard_ws.api_ws(Request()), timeout=5)  # type: ignore[arg-type]

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

            async def prepare(self, request) -> None:
                return None

            async def send_json(self, payload: dict) -> None:
                self.sent.append(payload)

            def __aiter__(self):
                return self

            async def __anext__(self):
                await recovered.wait()
                raise StopAsyncIteration

        fake_ws = FakeWebSocket()
        monkeypatch.setattr(dashboard_ws, "_check_ws_origin", lambda request: None)
        monkeypatch.setattr(dashboard_ws.web, "WebSocketResponse", lambda **kwargs: fake_ws)
        monkeypatch.setattr(source_providers, "CHECK_STATUS_TTL_SECS", 0.01)
        monkeypatch.setattr(source_providers, "schedule_check_refresh", refresh)

        await asyncio.wait_for(dashboard_ws.api_ws(Request()), timeout=5)  # type: ignore[arg-type]

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
            slot.append(
                "assistant", f"see https://github.com/acme/repo/pull/{n}", broadcast=False
            )
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

    def test_turn_boundary_forces_refresh_for_owner(self, state: DashboardState, monkeypatch) -> None:
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

    def test_turn_boundary_swallows_refresh_failures(
        self, state: DashboardState, monkeypatch
    ) -> None:
        """A status refresh is best-effort telemetry; it must never be able to
        break the turn-completion path it hangs off."""
        from kiro_crew.dashboard.handlers import source_providers

        slot = state.get_or_create_slot("chat-a")
        slot.append("assistant", "opened https://github.com/acme/repo/pull/7", broadcast=False)
        state._owner_ws_clients.add(MagicMock(closed=False))
        monkeypatch.setattr(
            source_providers,
            "request_check_refresh_now",
            MagicMock(side_effect=RuntimeError("no event loop")),
        )

        state.refresh_slot_source_status("chat-a")  # must not raise

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

        Regression for the production-wiring gap: the transport tests above call
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
