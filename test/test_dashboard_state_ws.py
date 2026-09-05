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


class TestSlotsBroadcastCarriesFolders:
    """The slots broadcast frame piggybacks the folder tree so the sidebar can
    group sessions on first paint without waiting for GET /api/chat/folders.

    A dashboard-user client receives the ``default_msg`` verbatim (the scope
    chokepoint only rebuilds the frame for app tokens), so ``folders`` on the
    broadcast note reaches it in the sent JSON. The frame must carry the cheap
    in-memory tree WITHOUT ``history_count`` (that field costs a synchronous
    session scan the broadcast hot path must never run)."""

    class _DashboardWS:
        """Minimal fake WS that reads as a dashboard user and captures sends.

        ``send_str`` is an ``AsyncMock`` so the fire-and-forget
        ``asyncio.ensure_future(ws.send_str(msg))`` in ``_spawn_ws_send`` records
        the call synchronously — the same pattern the other broadcast tests use
        to inspect a frame without draining the loop."""

        def __init__(self) -> None:
            self.closed = False
            self.send_str = AsyncMock()
            self._flags = {"_is_dashboard_user": True}

        def get(self, key, default=None):
            return self._flags.get(key, default)

    def test_frame_carries_folder_tree_without_counts(
        self, state: DashboardState
    ) -> None:
        state.serialize_slots = MagicMock(return_value=[{"key": "chat-1", "folder_id": "f1"}])  # type: ignore[method-assign]
        # In-memory folder tree as loaded from folders.json — no history_count.
        state._folders = [
            {"id": "f1", "name": "Work", "order": 0},
            {"id": "f2", "name": "Personal", "order": 1, "parent_id": "f1"},
        ]
        ws = self._DashboardWS()
        state.register_ws(ws)  # type: ignore[arg-type]

        state._do_slots_broadcast()

        ws.send_str.assert_called_once()
        frame = json.loads(ws.send_str.call_args[0][0])
        assert frame["type"] == "slots"
        assert frame["folders"] == state._folders
        # The expensive per-folder count must NOT ride this hot-path frame.
        assert all("history_count" not in f for f in frame["folders"])

    def test_malformed_folder_store_degrades_instead_of_crashing(
        self, state: DashboardState
    ) -> None:
        # A corrupt folders.json can leave _folders as a non-list, or a list with
        # non-dict / id-less entries. The broadcast must not crash; well-formed
        # entries survive and the rest are dropped.
        state.serialize_slots = MagicMock(return_value=[])  # type: ignore[method-assign]
        state._folders = [
            {"id": "ok", "name": "Keep", "order": 0},
            {"name": "no id", "order": 1},   # missing id -> dropped
            "not a dict",                     # non-dict -> dropped
            {"id": 42, "name": "int id"},     # non-string id -> dropped
        ]
        ws = self._DashboardWS()
        state.register_ws(ws)  # type: ignore[arg-type]

        state._do_slots_broadcast()  # must not raise

        frame = json.loads(ws.send_str.call_args[0][0])
        assert frame["folders"] == [{"id": "ok", "name": "Keep", "order": 0}]

    def test_non_list_folder_store_yields_empty_tree(
        self, state: DashboardState
    ) -> None:
        # A scalar/dict where a list is expected would make list() raise; the
        # coercion must yield [] instead of crashing the slot push.
        state.serialize_slots = MagicMock(return_value=[])  # type: ignore[method-assign]
        state._folders = {"corrupt": "mapping"}  # type: ignore[assignment]
        ws = self._DashboardWS()
        state.register_ws(ws)  # type: ignore[arg-type]

        state._do_slots_broadcast()  # must not raise

        frame = json.loads(ws.send_str.call_args[0][0])
        assert frame["folders"] == []

    def test_frame_carries_the_folder_generation(self, state: DashboardState) -> None:
        # The tree alone is not a change signal — this frame fires on routine
        # session activity — so the client needs the generation to tell "the
        # store changed" from "a session blinked".
        state.serialize_slots = MagicMock(return_value=[])  # type: ignore[method-assign]
        state._folders_generation = 3
        ws = self._DashboardWS()
        state.register_ws(ws)  # type: ignore[arg-type]

        state._do_slots_broadcast()

        frame = json.loads(ws.send_str.call_args[0][0])
        assert frame["foldersGeneration"] == 3

    def test_slot_activity_alone_does_not_advance_the_generation(
        self, state: DashboardState
    ) -> None:
        # THE guardrail. The client refetches whenever this number moves, so a
        # generation that crept up on ordinary slot churn would refetch the
        # session-scanning GET /api/chat/folders on every session event and land
        # that refetch over any in-flight optimistic folder edit.
        state.serialize_slots = MagicMock(return_value=[{"key": "chat-1"}])  # type: ignore[method-assign]
        state._folders = [{"id": "f1", "name": "Work", "order": 0}]
        ws = self._DashboardWS()
        state.register_ws(ws)  # type: ignore[arg-type]

        state._do_slots_broadcast()
        state.serialize_slots = MagicMock(  # type: ignore[method-assign]
            return_value=[{"key": "chat-1", "title": "renamed"}, {"key": "chat-2"}]
        )
        state._do_slots_broadcast()

        generations = [
            json.loads(call[0][0])["foldersGeneration"] for call in ws.send_str.call_args_list
        ]
        assert len(generations) == 2
        assert generations[0] == generations[1]

    @pytest.mark.asyncio
    async def test_folder_mutation_advances_the_generation(
        self, state: DashboardState
    ) -> None:
        # Bumped in the mutate_folders funnel rather than at each call site, so a
        # new folder-writing endpoint cannot forget to do it.
        before = state.folders_generation()

        await state.mutate_folders(
            lambda folders: (True, folders.append({"id": "f9", "name": "New", "order": 0}))
        )

        state.serialize_slots = MagicMock(return_value=[])  # type: ignore[method-assign]
        ws = self._DashboardWS()
        state.register_ws(ws)  # type: ignore[arg-type]
        state._do_slots_broadcast()

        frame = json.loads(ws.send_str.call_args[0][0])
        assert frame["foldersGeneration"] == before + 1
        assert frame["folders"] == [{"id": "f9", "name": "New", "order": 0}]

    @pytest.mark.asyncio
    async def test_a_no_op_folder_transaction_does_not_advance_the_generation(
        self, state: DashboardState
    ) -> None:
        # `changed=False` returns before the write; nothing changed, so no client
        # should be told to refetch.
        before = state.folders_generation()

        await state.mutate_folders(lambda folders: (False, None))

        assert state.folders_generation() == before

    @pytest.mark.asyncio
    async def test_a_failed_folder_transaction_does_not_advance_the_generation(
        self, state: DashboardState
    ) -> None:
        before = state.folders_generation()
        state._folders = [{"id": "f1", "name": "Existing", "order": 0}]

        def fail_write(*_args: object) -> None:
            raise OSError("disk full")

        state._write_folders_confirmed = fail_write  # type: ignore[method-assign]

        with pytest.raises(OSError, match="disk full"):
            await state.mutate_folders(
                lambda folders: (
                    True,
                    folders.append({"id": "f2", "name": "Rolled back", "order": 1}),
                )
            )

        assert state.folders_generation() == before
        assert state._folders == [{"id": "f1", "name": "Existing", "order": 0}]


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


class TestSlotEffectiveAgent:
    """`to_dict()["effective_agent"]` — the non-destructive divergence report.

    The contract is asymmetric on purpose. ``agent`` is the user's INTENT and is
    stored verbatim; ``effective_agent`` is a claim that something else answers,
    and is emitted ONLY when that is known to be true. Every "we cannot tell yet"
    case therefore has to come back empty: a false "your agent was substituted"
    marker sends the user chasing a substitution that never happened, which is
    strictly worse than a boot window with no marker at all.

    The other pinned property is that resolution touches NO filesystem. It runs
    inside every slots frame on the event loop, so a scan here would be a
    recurring gateway stall — the tests below make the scanning entry points
    explode and still expect an answer.
    """

    @staticmethod
    def _pin(monkeypatch, *, aliases, default_alias, materialized, ready=True):
        """Pin both in-memory snapshots, isolating from the host's real config.

        Without this the resolver reads whatever ``~/.kiro/agents`` and the last
        ``load()`` left behind, so a developer host with a real agent called
        "researcher" would answer differently from CI.
        """
        import kiro_crew.config.loader as loader

        monkeypatch.setattr(
            loader,
            "_CONFIG_AGENT_ALIAS_SNAPSHOT",
            (frozenset(aliases), default_alias, ready),
        )
        monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS", frozenset(materialized))
        monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS_READY", ready)

    def test_honored_alias_reports_nothing(self, monkeypatch) -> None:
        from kiro_crew.dashboard.state import _ChatSlot

        self._pin(
            monkeypatch,
            aliases={"kirocrew", "researcher"},
            default_alias="kirocrew",
            materialized=set(),
        )
        slot = _ChatSlot("s1", agent="researcher")
        d = slot.to_dict()
        assert d["effective_agent"] == ""
        # The request is never rewritten by the report.
        assert d["agent"] == "researcher"

    def test_materialized_kiro_agent_reports_nothing(self, monkeypatch) -> None:
        """An app agent lives in ~/.kiro/agents, never in config.agents."""
        from kiro_crew.dashboard.state import _ChatSlot

        self._pin(
            monkeypatch,
            aliases={"kirocrew"},
            default_alias="kirocrew",
            materialized={"mochi"},
        )
        slot = _ChatSlot("s1", agent="mochi")
        assert slot.to_dict()["effective_agent"] == ""

    def test_unresolvable_agent_names_the_fallback(self, monkeypatch) -> None:
        from kiro_crew.dashboard.state import _ChatSlot

        self._pin(
            monkeypatch,
            aliases={"kirocrew"},
            default_alias="kirocrew",
            materialized={"mochi"},
        )
        slot = _ChatSlot("s1", agent="deleted-app")
        d = slot.to_dict()
        assert d["effective_agent"] == "kirocrew"
        # Still verbatim: the marker describes, it does not normalize.
        assert d["agent"] == "deleted-app"

    def test_blank_agent_reports_nothing(self, monkeypatch) -> None:
        from kiro_crew.dashboard.state import _ChatSlot

        self._pin(
            monkeypatch,
            aliases={"kirocrew"},
            default_alias="kirocrew",
            materialized=set(),
        )
        assert _ChatSlot("s1").to_dict()["effective_agent"] == ""

    def test_cold_materialized_snapshot_reports_nothing(self, monkeypatch) -> None:
        """The boot window. A cold snapshot cannot see app agents that DO exist."""
        import kiro_crew.config.loader as loader
        from kiro_crew.dashboard.state import _ChatSlot

        self._pin(
            monkeypatch,
            aliases={"kirocrew"},
            default_alias="kirocrew",
            materialized=set(),
        )
        monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS_READY", False)
        slot = _ChatSlot("s1", agent="mochi")
        assert slot.to_dict()["effective_agent"] == ""

    def test_cold_alias_snapshot_reports_nothing(self, monkeypatch) -> None:
        """No load() has published yet, so there is no fallback name to name."""
        from kiro_crew.dashboard.state import _ChatSlot

        self._pin(
            monkeypatch,
            aliases=set(),
            default_alias="",
            materialized=set(),
            ready=False,
        )
        slot = _ChatSlot("s1", agent="mochi")
        assert slot.to_dict()["effective_agent"] == ""

    def test_agent_equal_to_the_fallback_reports_nothing(self, monkeypatch) -> None:
        """Nothing diverged: the name requested IS the one answering."""
        import kiro_crew.config.loader as loader

        self._pin(
            monkeypatch,
            aliases=set(),
            default_alias="kirocrew",
            materialized=set(),
        )
        assert loader.resolve_effective_agent("kirocrew") == ""

    def test_project_declared_agent_reports_nothing(self, monkeypatch) -> None:
        """A project's own .kiro scope resolves the name; that is not divergence."""
        import kiro_crew.agent_discovery as discovery
        import kiro_crew.config.loader as loader
        from kiro_crew.dashboard.state import _ChatSlot

        self._pin(
            monkeypatch,
            aliases={"kirocrew"},
            default_alias="kirocrew",
            materialized=set(),
        )
        monkeypatch.setattr(
            discovery,
            "cached_project_agent_names",
            lambda _d: frozenset({"repo-agent"}),
        )
        slot = _ChatSlot("s1", agent="repo-agent")
        slot.project = "/some/checkout"
        assert slot.to_dict()["effective_agent"] == ""
        # Same pinning, a name the warm cache does NOT declare: now it diverges.
        other = _ChatSlot("s2", agent="gone")
        other.project = "/some/checkout"
        assert other.to_dict()["effective_agent"] == "kirocrew"
        assert loader is not None

    def test_cold_project_cache_reports_nothing(self, monkeypatch) -> None:
        """An uncached project is not evidence of absence."""
        import kiro_crew.agent_discovery as discovery
        from kiro_crew.dashboard.state import _ChatSlot

        self._pin(
            monkeypatch,
            aliases={"kirocrew"},
            default_alias="kirocrew",
            materialized=set(),
        )
        monkeypatch.setattr(discovery, "cached_project_agent_names", lambda _d: None)
        slot = _ChatSlot("s1", agent="repo-agent")
        slot.project = "/some/checkout"
        assert slot.to_dict()["effective_agent"] == ""

    def test_resolution_never_touches_the_filesystem(self, monkeypatch) -> None:
        """The loop-safety property, as a gate rather than a comment.

        Both scanning entry points are made to explode. `to_dict` runs on the
        event loop for every slots frame, so reaching either one would be a
        per-frame stall — the kind the loop-stall watchdog blames on chat.
        """
        import kiro_crew.agent_discovery as discovery
        import kiro_crew.config.loader as loader
        from kiro_crew.dashboard.state import _ChatSlot

        self._pin(
            monkeypatch,
            aliases={"kirocrew"},
            default_alias="kirocrew",
            materialized=set(),
        )

        def _boom(*_a, **_k):  # pragma: no cover - must never run
            raise AssertionError("effective-agent resolution scanned the filesystem")

        monkeypatch.setattr(loader, "refresh_materialized_agents", _boom)
        monkeypatch.setattr(loader, "_scan_materialized_agents", _boom)
        monkeypatch.setattr(discovery, "project_agent_names", _boom)
        monkeypatch.setattr(discovery, "cached_project_agent_names", lambda _d: frozenset())

        slot = _ChatSlot("s1", agent="deleted-app")
        slot.project = "/some/checkout"
        assert slot.to_dict()["effective_agent"] == "kirocrew"

    def test_project_lookup_failure_reports_nothing(self, monkeypatch) -> None:
        """A broken cache read is "no evidence", never an exception into a frame."""
        import kiro_crew.agent_discovery as discovery
        from kiro_crew.dashboard.state import _ChatSlot

        self._pin(
            monkeypatch,
            aliases={"kirocrew"},
            default_alias="kirocrew",
            materialized=set(),
        )

        def _raise(_d):
            raise RuntimeError("cache exploded")

        monkeypatch.setattr(discovery, "cached_project_agent_names", _raise)
        slot = _ChatSlot("s1", agent="deleted-app")
        slot.project = "/some/checkout"
        assert slot.to_dict()["effective_agent"] == ""

    def test_load_publishes_the_alias_snapshot(self, monkeypatch) -> None:
        """`load()` is the publisher, so the resolver never reads config.json."""
        import kiro_crew.config.loader as loader

        monkeypatch.setattr(
            loader, "_CONFIG_AGENT_ALIAS_SNAPSHOT", (frozenset(), "", False)
        )

        cfg = loader.KiroCrewConfig.load()
        aliases, default_alias, ready = loader.agent_alias_snapshot()
        assert ready is True
        assert aliases == frozenset(cfg.agents)
        assert default_alias in cfg.agents

    def test_publish_overwrites_a_richer_previous_snapshot(self, monkeypatch) -> None:
        """The degraded-defaults path must SHRINK the snapshot, not union into it.

        Leaving a stale alias published would have the resolver honor a name that
        no longer loads, which is the false-negative twin of a false marker.
        """
        import dataclasses

        import kiro_crew.config.loader as loader

        self._pin(
            monkeypatch,
            aliases={"kirocrew", "researcher"},
            default_alias="kirocrew",
            materialized=set(),
        )
        cfg = loader.KiroCrewConfig()
        cfg = dataclasses.replace(
            cfg,
            agents={"default": loader.KiroCrewAgentConfig(kiro_agent="kirocrew")},
            default_agent="default",
        )
        loader.publish_agent_alias_snapshot(cfg)
        aliases, default_alias, _ = loader.agent_alias_snapshot()
        assert aliases == frozenset({"default"})
        assert default_alias == "default"
        assert loader.resolve_effective_agent("researcher") == "default"

    def test_snapshot_is_published_as_one_immutable_triple(self, monkeypatch) -> None:
        """Why the read path needs no lock, as a gate rather than a comment.

        A reader loads ONE name, so it sees either the whole old triple or the
        whole new one. Split across three globals it could pair a fresh alias set
        with a stale fallback name, and the fix for that would be a mutex acquired
        once per slot per frame on the event loop.
        """
        import kiro_crew.config.loader as loader

        monkeypatch.setattr(
            loader, "_CONFIG_AGENT_ALIAS_SNAPSHOT", (frozenset(), "", False)
        )
        before = loader.agent_alias_snapshot()
        loader.publish_agent_alias_snapshot(loader.KiroCrewConfig.load())
        after = loader.agent_alias_snapshot()

        assert isinstance(after, tuple) and len(after) == 3
        assert isinstance(after[0], frozenset)
        # Swapped wholesale, so the value a reader already holds is unchanged.
        assert before == (frozenset(), "", False)
        assert after is not before


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
    def test_public_repo_status_rides_general_frame_owner_gets_full(
        self, state: DashboardState, monkeypatch
    ) -> None:
        source_url = "https://github.com/acme/repo/pull/12"

        def serialize_slots(
            *, include_check_status: bool = False, dashboard_user: bool = False
        ) -> list[dict]:
            link = {"url": source_url, "provider": "github", "number": 12}
            # Owner (include_check_status) sees status for any repo. A
            # dashboard-user sees it for a KNOWN-public repo; this fixture treats
            # dashboard_user=True as "public repo, show status".
            if include_check_status or dashboard_user:
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

        class _FakeWs:
            def __init__(self, *, dashboard_user: bool) -> None:
                self.closed = False
                self._flags = {"_is_dashboard_user": dashboard_user}

            def get(self, key, default=None):
                return self._flags.get(key, default)

        dash_ws = _FakeWs(dashboard_user=True)
        owner_ws = _FakeWs(dashboard_user=True)
        state.register_ws(dash_ws)
        state.register_ws(owner_ws, owner=True)
        sse_queue = state.register_sse()

        state.push_slots_update()

        # SSE carries the BARE list: the SSE queue has NO per-app filtering, so
        # status must never ride it (GPT #6789 — an app token on /api/stream
        # would otherwise receive credential-backed chip status). The enriched
        # list is carried separately in `_slots_list_ws` for the WS path only.
        sse_note = sse_queue.get_nowait()
        assert "ci" not in str(sse_note["_slots_list"])
        assert "state" not in sse_note["_slots_list"][0]["source_links"][0]
        # The enriched WS-only list DOES carry status (delivered to WS
        # dashboard-user sockets, re-filtered for app tokens in
        # `_serialize_for_client`).
        assert sse_note["_slots_list_ws"][0]["source_links"][0]["ci"] == "passed"
        assert sse_note["_slots_list_ws"][0]["source_links"][0]["state"] == "OPEN"

        dash_messages = [message for client, message in sent if client is dash_ws]
        owner_messages = [message for client, message in sent if client is owner_ws]
        # Dashboard user (non-owner): the single general frame carries public-repo
        # status. `_send_ws_all` delivers exactly one `slots` frame to it.
        assert len(dash_messages) == 1
        assert dash_messages[0]["data"][0]["source_links"][0]["ci"] == "passed"
        assert dash_messages[0]["data"][0]["source_links"][0]["state"] == "OPEN"
        # Owner: EXACTLY ONE frame (the enriched owner frame). `_send_ws_all`
        # skips owner sockets for `slots` (PR #6795), so the generic frame no
        # longer backstops an owner; the owner frame is its only one and carries
        # full status.
        assert len(owner_messages) == 1
        assert owner_messages[0]["data"][0]["source_links"][0]["ci"] == "passed"
        assert owner_messages[0]["data"][0]["source_links"][0]["state"] == "OPEN"
        # Both frames come from `_slots_ws_frame`, so they must carry the SAME
        # envelope keys — asserted as set equality so a key added to one site and
        # not the other fails here instead of silently depriving an owner window.
        assert set(owner_messages[0]) == set(dash_messages[0])
        assert owner_messages[0]["folders"] == dash_messages[0]["folders"]
        assert (
            owner_messages[0]["gitlabHostsGeneration"]
            == dash_messages[0]["gitlabHostsGeneration"]
        )
        assert isinstance(owner_messages[0]["governanceGeneration"], int)
        assert (
            owner_messages[0]["governanceGeneration"]
            == dash_messages[0]["governanceGeneration"]
        )

    def test_owner_sockets_still_receive_non_slot_broadcasts(
        self, state: DashboardState, monkeypatch
    ) -> None:
        """The `slots` exclusion must not silence an owner socket generally.

        The generic fan-out skips owner sockets for `slots` alone, because that is
        the one message type they receive twice. Every other type has no
        owner-specific frame to fall back on, so a skip that keyed on the socket
        rather than on the message type would drop it entirely — an owner window
        that stops seeing refreshes, with nothing raised anywhere. This is the
        control for that: it fails if the exclusion is ever widened.
        """
        sent: list[tuple[object, dict]] = []
        monkeypatch.setattr(
            state,
            "_spawn_ws_send",
            lambda client, message: sent.append((client, json.loads(message))),
        )
        owner_ws = MagicMock(closed=False)
        state.register_ws(owner_ws, owner=True)

        state._broadcast({"_type": "refresh", "kinds": "crons,agents"})

        owner_messages = [message for client, message in sent if client is owner_ws]
        assert len(owner_messages) == 1
        assert owner_messages[0]["type"] == "refresh"
        assert owner_messages[0]["data"]["kinds"] == ["crons", "agents"]

    def test_app_token_frame_is_stripped_of_chip_status(self) -> None:
        # The per-app filter must remove credential-backed chip status even when
        # the general list carries it (widened for dashboard users). Slot
        # metadata stays; ci/state/mergeable go.
        from kiro_crew.dashboard.ws_event_scope import _strip_source_link_status

        slot = {
            "key": "chat-1",
            "source_links": [
                {
                    "url": "https://github.com/acme/repo/pull/12",
                    "provider": "github",
                    "number": 12,
                    "ci": "passed",
                    "state": "merged",
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                }
            ],
        }
        cleaned = _strip_source_link_status(slot)
        link = cleaned["source_links"][0]
        assert link["url"].endswith("/pull/12")
        assert link["number"] == 12
        for k in ("ci", "state", "mergeable", "mergeStateStatus"):
            assert k not in link
        # A slot with no status is returned unchanged (identity, no copy).
        bare = {"key": "c", "source_links": [{"url": "u", "number": 1}]}
        assert _strip_source_link_status(bare) is bare

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
        from kiro_crew.dashboard import ws_event_scope
        from kiro_crew.dashboard.handlers import source_providers

        # An app token only exists for an INSTALLED, enabled app, so that is the
        # world this test runs in. The connect path resolves enablement off-loop
        # and refuses a disabled app outright; without this the synthetic
        # ``source-app`` (absent from the real installed.json) would be rejected
        # before the initial status push this test is about.
        monkeypatch.setattr(ws_event_scope, "is_app_enabled", lambda _name: True)
        monkeypatch.setattr(ws_event_scope, "get_app_manifest", lambda _name: None)
        # The connect read PRIMES the process-wide cache, so give it a throwaway
        # dict -- monkeypatch restores the real one and no entry leaks into a
        # later test that shares the app name.
        monkeypatch.setattr(ws_event_scope, "_declared_cache", {})

        source_url = "https://github.com/acme/repo/pull/12"

        def serialize_slots(
            *, include_check_status: bool = False, dashboard_user: bool = False
        ) -> list[dict]:
            link = {"url": source_url, "provider": "github", "number": 12}
            # This connect test seeds no repo visibility, so a dashboard user
            # fails closed to a bare chip (only the owner opt-in carries status).
            if include_check_status:
                link.update({"ci": "passed", "state": "OPEN"})
            return [{"key": "chat-1", "source_links": [link]}]

        state = MagicMock()
        state.owner_id = "U_OWNER"
        state.serialize_slots.side_effect = serialize_slots
        state._yolo = False
        # Real folder list (a MagicMock attr would coerce to [] via
        # _safe_folder_tree); lets the dashboard-user branch below assert the
        # connect-time frame carries the folder tree — the frame that fixes the
        # first-paint flicker (#4127).
        state._folders = [{"id": "f1", "name": "Work", "order": 0}]

        class Request(dict):
            def __init__(self) -> None:
                super().__init__(claims)
                # Mirror the auth middleware: it sets this POSITIVE flag so the
                # WS layer never infers trust from a falsy app claim.
                self.setdefault("is_dashboard_user", not self.get("app"))
                self.app = {"state": state}

        class FakeWebSocket:
            def __init__(self) -> None:
                self.closed = True
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

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        fake_ws = FakeWebSocket()
        refresh = MagicMock()
        vis_refresh = MagicMock()
        monkeypatch.setattr(dashboard_ws, "_check_ws_origin", lambda request: None)
        monkeypatch.setattr(dashboard_ws.web, "WebSocketResponse", lambda **kwargs: fake_ws)
        monkeypatch.setattr(source_providers, "schedule_check_refresh", refresh)
        monkeypatch.setattr(source_providers, "schedule_visibility_refresh", vis_refresh)

        result = await dashboard_ws.api_ws(Request())  # type: ignore[arg-type]
        await asyncio.sleep(0)

        assert result is fake_ws
        state.register_ws.assert_called_once_with(fake_ws, owner=owner_request)
        initial_frame = fake_ws.sent[0]
        initial_slots = initial_frame["data"]
        if owner_request:
            assert initial_slots[0]["source_links"][0]["ci"] == "passed"
            refresh.assert_called_once_with([source_url], state.push_slots_update)
            vis_refresh.assert_called_once_with([source_url], state.push_slots_update)
        elif claims.get("app"):
            # App token: the per-app WS scope gate filters the initial push, so
            # an app that declared no slots:* scope sees no slots at all — a
            # stronger guarantee than merely withholding check status. No
            # provider work is scheduled for it.
            assert initial_slots == []
            refresh.assert_not_called()
            vis_refresh.assert_not_called()
            # Folders never ride an app-token frame (apps don't render the tree).
            assert "folders" not in initial_frame
        else:
            # Non-owner dashboard user: connect frame carries NO status (this
            # test seeds no visibility, so the public gate fails closed), and the
            # connection MUST drive NEITHER refresh — both the status read and
            # the visibility probe run the operator's credentials, so only the
            # owner's connection may trigger them (GPT round-13). A non-owner
            # renders the owner-populated caches read-only; it spawns no provider
            # work of its own.
            assert "ci" not in str(initial_slots)
            assert "state" not in initial_slots[0]["source_links"][0]
            refresh.assert_not_called()
            vis_refresh.assert_not_called()
            # The connect-time frame is what populates the sidebar on a cold
            # load, so a dashboard user MUST receive the folder tree here — this
            # is the frame that fixes the #4127 flicker.
            assert initial_frame["folders"] == [{"id": "f1", "name": "Work", "order": 0}]
        state.unregister_ws.assert_called_once_with(fake_ws)

    @pytest.mark.asyncio
    async def test_non_owner_status_refresh_only_for_confirmed_public(
        self, monkeypatch
    ) -> None:
        """A non-owner dashboard connection drives NEITHER a status refresh NOR
        a visibility probe, even when a confirmed-public repo is present — both
        run the operator's credentials, so only the owner's connection may
        trigger them (GPT round-13). The non-owner renders the owner-populated
        caches read-only."""
        from kiro_crew.dashboard import ws as dashboard_ws
        from kiro_crew.dashboard import ws_event_scope
        from kiro_crew.dashboard.handlers import source_providers

        monkeypatch.setattr(ws_event_scope, "is_app_enabled", lambda _name: True)
        monkeypatch.setattr(ws_event_scope, "get_app_manifest", lambda _name: None)
        monkeypatch.setattr(ws_event_scope, "_declared_cache", {})

        public_url = "https://github.com/acme/public/pull/1"
        private_url = "https://github.com/acme/private/pull/2"

        def serialize_slots(
            *, include_check_status: bool = False, dashboard_user: bool = False
        ) -> list[dict]:
            return [
                {
                    "key": "chat-1",
                    "source_links": [
                        {"url": public_url, "provider": "github", "number": 1},
                        {"url": private_url, "provider": "github", "number": 2},
                    ],
                }
            ]

        state = MagicMock()
        state.owner_id = "U_OWNER"
        state.serialize_slots.side_effect = serialize_slots
        state._yolo = False
        state._folders = []

        class Request(dict):
            def __init__(self) -> None:
                super().__init__({"user": "U_OTHER", "app": ""})
                self.setdefault("is_dashboard_user", True)
                self.app = {"state": state}

        class FakeWebSocket:
            def __init__(self) -> None:
                self.closed = True
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

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        fake_ws = FakeWebSocket()
        refresh = MagicMock()
        vis_refresh = MagicMock()
        monkeypatch.setattr(dashboard_ws, "_check_ws_origin", lambda request: None)
        monkeypatch.setattr(dashboard_ws.web, "WebSocketResponse", lambda **kwargs: fake_ws)
        monkeypatch.setattr(source_providers, "schedule_check_refresh", refresh)
        monkeypatch.setattr(source_providers, "schedule_visibility_refresh", vis_refresh)

        result = await dashboard_ws.api_ws(Request())  # type: ignore[arg-type]
        await asyncio.sleep(0)

        assert result is fake_ws
        # Owner-only model: a non-owner triggers NO provider work at all — not a
        # status refresh and not a visibility probe — regardless of whether a
        # repo would read public. The owner's connection populates the caches.
        vis_refresh.assert_not_called()
        refresh.assert_not_called()


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
                source_providers._publish_provider_hosts(frozenset({"gitlab.acme.internal"}), frozenset())
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

            def __aiter__(self):
                return self

            async def __anext__(self):
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

        await asyncio.wait_for(dashboard_ws.api_ws(Request()), timeout=5)  # type: ignore[arg-type]

        refresh.assert_not_called()
        state.source_link_urls.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_owner_dashboard_user_does_not_start_refresh_loop(self, monkeypatch) -> None:
        """A signed-in dashboard user who is NOT the owner renders PUBLIC-repo
        chip status READ-ONLY from the owner-populated caches, so it must NOT
        start the periodic driver — both the status and visibility refreshes run
        the operator's credentials and are owner-only (GPT round-13)."""
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

            def __aiter__(self):
                return self

            async def __anext__(self):
                await asyncio.sleep(0.05)
                raise StopAsyncIteration

        fake_ws = FakeWebSocket()
        monkeypatch.setattr(dashboard_ws, "_check_ws_origin", lambda request: None)
        monkeypatch.setattr(dashboard_ws.web, "WebSocketResponse", lambda **kwargs: fake_ws)
        monkeypatch.setattr(source_providers, "CHECK_STATUS_TTL_SECS", 0.01)
        monkeypatch.setattr(source_providers, "schedule_check_refresh", check_refresh)
        monkeypatch.setattr(source_providers, "schedule_visibility_refresh", vis_refresh)

        await asyncio.wait_for(dashboard_ws.api_ws(Request()), timeout=5)  # type: ignore[arg-type]
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

    def test_turn_boundary_non_owner_drives_no_refresh(
        self, state: DashboardState, monkeypatch
    ) -> None:
        """GPT #6789 round-13: at a turn boundary with ONLY a non-owner
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
