"""Dashboard chat: multi-agent-orchestration runtime wiring of the slot agent and workspace."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app_with_agent_routes, _make_state
from dashboard_owner_helpers import as_owner

from kiro_crew.config.loader import ResolvedBindings

# ── Runtime wiring tests (multi-agent-orchestration) ──


class TestRuntimeWiring:
    """Tests for multi-agent-orchestration runtime wiring.

    Requirements: 1.3, 2.3, 2.4, 3.1
    """

    @pytest.mark.asyncio
    async def test_api_chat_slot_agent_response_includes_workspace(self, tmp_path, monkeypatch):
        """api_chat_slot_agent response includes resolved workspace field.

        Requirements: 1.3
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        state.sessions.reset = AsyncMock()

        # Mock config loading to return a config with a known agent
        mock_cfg = MagicMock()
        mock_cfg.agents = {"oncall": MagicMock(workspace="oncall-ws", memory_store="oncall-mem")}
        mock_cfg.workspaces = {"oncall-ws": MagicMock(dir="/tmp/oncall")}
        mock_cfg.default_workspace = "default"
        mock_cfg.default_memory_store = "default"
        mock_cfg.memory_stores = {"oncall-mem": MagicMock()}
        mock_cfg.memory = MagicMock()

        mock_bindings = ResolvedBindings(
            workspace_dir=tmp_path,
            memory_store_name="",
            effective_memory_config={},
            kiro_agent="kirocrew",
            selection_kind="template",
        )
        mock_bindings.workspace_dir = Path("/tmp/oncall")
        mock_bindings.memory_store_name = "oncall-mem"
        mock_bindings.model = ""

        monkeypatch.setattr("kiro_crew.dashboard.chat.KiroCrewConfig.load", lambda: mock_cfg)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", lambda: mock_cfg
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat.resolve_agent_bindings",
            lambda cfg, name, project_dir=None, **kwargs: mock_bindings,
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.resolve_agent_bindings",
            lambda cfg, name, project_dir=None, **kwargs: mock_bindings,
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat._workspace_name_for_dir",
            lambda cfg, ws_dir: "oncall-ws",
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._workspace_name_for_dir",
            lambda cfg, ws_dir: "oncall-ws",
        )

        async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
            resp = await client.post("/api/chat/slots/s1/agent", json={"agent": "oncall"})
            data = await resp.json()
            assert resp.status == 200
            assert data["ok"] is True
            assert data["agent"] == "oncall"
            assert "workspace" in data
            assert data["workspace"] == "oncall-ws"

    @pytest.mark.asyncio
    async def test_api_chat_slot_agent_failed_resolution_reports_slot_workspace(
        self, tmp_path, monkeypatch
    ):
        """A binding-resolution failure must not fabricate workspace='default'.

        The handler logs and proceeds when resolve_agent_bindings raises, and
        slot.workspace keeps its previous value — so the response must name
        THAT value. The acting tab writes the response's workspace
        into its store optimistically; a fabricated 'default' would pin the
        chip to a workspace the slot does not hold, and with the websocket
        down (the optimistic write's whole premise) nothing corrects it.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.workspace = "research-ws"
        state.sessions.reset = AsyncMock()

        def _boom():
            raise RuntimeError("config unreadable")

        monkeypatch.setattr("kiro_crew.dashboard.chat.KiroCrewConfig.load", _boom)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", _boom)

        async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
            resp = await client.post("/api/chat/slots/s1/agent", json={"agent": "oncall"})
            data = await resp.json()
            assert resp.status == 200
            assert data["ok"] is True
            assert data["agent"] == "oncall"
            # The slot's workspace was not changed by the failed resolution,
            # and the response reports the value the slot actually holds.
            assert slot.workspace == "research-ws"
            assert data["workspace"] == "research-ws"

    @pytest.mark.asyncio
    async def test_api_chat_slot_agent_reset_failure_keeps_committed_agent(
        self, tmp_path, monkeypatch
    ):
        """A throwing teardown reports SUCCESS with a warning; the agent stays.

        The reset pops the session before its shutdown can fail, so by the
        failure point the old binding's session does not exist — every
        future or replacement session cold-starts on the NEW agent. The
        switch therefore succeeded: answering 500 would make the acting
        tab's performSlotSwitch keep the OLD store value for a switch that
        actually happened.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.agent = "oncall"
        slot.workspace = "oncall-ws"
        slot.project = "/tmp/oncall"
        old = MagicMock()
        old.has_active_turn.return_value = False
        state.sessions.get_provider = MagicMock(return_value=old)

        async def _pop_then_raise(*_a, **_k):
            # The canonical post-pop shape: the provider is deregistered
            # before the shutdown raises, so the helper's identity probe
            # classifies the raise as post-pop and the handler answers the
            # committed switch.
            state.sessions.get_provider = MagicMock(return_value=None)
            raise RuntimeError("shutdown blew up")

        state.sessions.reset = AsyncMock(side_effect=_pop_then_raise)

        mock_cfg = MagicMock()
        mock_cfg.agents = {"research": MagicMock(workspace="research-ws", memory_store="default")}
        mock_cfg.workspaces = {"research-ws": MagicMock(dir="/tmp/research")}
        mock_cfg.default_workspace = "default"
        mock_cfg.default_memory_store = "default"
        mock_cfg.memory_stores = {}
        mock_cfg.memory = MagicMock()
        mock_bindings = ResolvedBindings(
            workspace_dir=tmp_path,
            memory_store_name="",
            effective_memory_config={},
            kiro_agent="kirocrew",
            selection_kind="template",
        )
        mock_bindings.workspace_dir = Path("/tmp/research")
        mock_bindings.memory_store_name = "default"
        mock_bindings.model = ""
        monkeypatch.setattr("kiro_crew.dashboard.chat.KiroCrewConfig.load", lambda: mock_cfg)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", lambda: mock_cfg
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat.resolve_agent_bindings",
            lambda cfg, name, project_dir=None, **kwargs: mock_bindings,
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.resolve_agent_bindings",
            lambda cfg, name, project_dir=None, **kwargs: mock_bindings,
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat._workspace_name_for_dir",
            lambda cfg, ws_dir: "research-ws",
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._workspace_name_for_dir",
            lambda cfg, ws_dir: "research-ws",
        )

        async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
            resp = await client.post("/api/chat/slots/s1/agent", json={"agent": "research"})
            data = await resp.json()
            # Success with a warning — the switch happened; only the old
            # process's teardown degraded.
            assert resp.status == 200
            assert data["ok"] is True
            assert data["agent"] == "research"
            assert data["warning"] == "old session teardown incomplete"
            # The committed bindings stay (the popped old session cannot come
            # back) — agent and the derived workspace both advertise the new
            # binding.
            assert slot.agent == "research"
            assert slot.workspace == "research-ws"

    @pytest.mark.asyncio
    async def test_api_chat_slot_agent_failed_reset_spares_concurrent_writes(
        self, tmp_path, monkeypatch
    ):
        """A failed switch never disturbs values written while it was in flight.

        The handler commits before the reset and its failure path touches
        nothing further, so state
        written by a concurrent actor during the (failing) reset — e.g. the
        project endpoint, which does not take the slot lock — survives
        untouched. Restoring captured priors here (the old rollback shape)
        would silently erase that concurrent success.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.agent = "oncall"
        slot.workspace = "oncall-ws"
        slot.project = "/tmp/oncall"

        async def _concurrent_switch_lands_then_reset_fails(*args, **kwargs):
            # Simulate a concurrent request winning the race while this
            # request's reset is in flight.
            slot.agent = "writer"
            slot.workspace = "writing-ws"
            slot.project = "/tmp/writing"
            raise RuntimeError("shutdown blew up")

        state.sessions.reset = AsyncMock(side_effect=_concurrent_switch_lands_then_reset_fails)
        # No live session was ever registered for the key, so the helper's
        # identity probe classifies the raise as post-pop and answers the
        # committed switch rather than a 500.
        state.sessions.get_provider = MagicMock(return_value=None)

        mock_cfg = MagicMock()
        mock_cfg.agents = {"research": MagicMock(workspace="research-ws", memory_store="default")}
        mock_cfg.workspaces = {"research-ws": MagicMock(dir="/tmp/research")}
        mock_cfg.default_workspace = "default"
        mock_cfg.default_memory_store = "default"
        mock_cfg.memory_stores = {}
        mock_cfg.memory = MagicMock()
        mock_bindings = ResolvedBindings(
            workspace_dir=tmp_path,
            memory_store_name="",
            effective_memory_config={},
            kiro_agent="kirocrew",
            selection_kind="template",
        )
        mock_bindings.workspace_dir = Path("/tmp/research")
        mock_bindings.memory_store_name = "default"
        mock_bindings.model = ""
        monkeypatch.setattr("kiro_crew.dashboard.chat.KiroCrewConfig.load", lambda: mock_cfg)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", lambda: mock_cfg
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat.resolve_agent_bindings",
            lambda cfg, name, project_dir=None, **kwargs: mock_bindings,
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.resolve_agent_bindings",
            lambda cfg, name, project_dir=None, **kwargs: mock_bindings,
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat._workspace_name_for_dir",
            lambda cfg, ws_dir: "research-ws",
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._workspace_name_for_dir",
            lambda cfg, ws_dir: "research-ws",
        )

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/agent", json={"agent": "research"})
            assert resp.status == 200
            # The concurrent winner's values survive — no field snaps back to
            # this request's priors.
            assert slot.agent == "writer"
            assert slot.workspace == "writing-ws"
            assert slot.project == "/tmp/writing"

    def _patch_agent_resolution(self, monkeypatch):
        """Stub config + binding resolution so the agent switch reaches its reset."""
        mock_cfg = MagicMock()
        mock_cfg.agents = {"research": MagicMock(workspace="research-ws", memory_store="default")}
        mock_cfg.workspaces = {"research-ws": MagicMock(dir="/tmp/research")}
        mock_cfg.default_workspace = "default"
        mock_cfg.default_memory_store = "default"
        mock_cfg.memory_stores = {}
        mock_cfg.memory = MagicMock()
        mock_bindings = ResolvedBindings(
            workspace_dir=Path("/tmp/research"),
            memory_store_name="",
            effective_memory_config={},
            kiro_agent="kirocrew",
            selection_kind="template",
        )
        mock_bindings.workspace_dir = Path("/tmp/research")
        mock_bindings.memory_store_name = "default"
        mock_bindings.model = ""
        monkeypatch.setattr("kiro_crew.dashboard.chat.KiroCrewConfig.load", lambda: mock_cfg)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", lambda: mock_cfg
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat.resolve_agent_bindings",
            lambda cfg, name, project_dir=None, **kwargs: mock_bindings,
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.resolve_agent_bindings",
            lambda cfg, name, project_dir=None, **kwargs: mock_bindings,
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat._workspace_name_for_dir",
            lambda cfg, ws_dir: "research-ws",
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._workspace_name_for_dir",
            lambda cfg, ws_dir: "research-ws",
        )

    @pytest.mark.asyncio
    async def test_api_chat_slot_agent_reset_raise_before_pop_propagates(
        self, tmp_path, monkeypatch
    ):
        """A raise with the session STILL REGISTERED propagates as a 500.

        The SAME provider instance registered before and after the raise
        means the failure came BEFORE the session pop (e.g. in the
        pending-wait unblock): the old session survives on the old agent, so
        a 200 + warning would report a switch that did not take. The
        committed values are rolled back before the raise escapes — the
        acting tab keeps its old store value on a non-2xx, and the probe has
        proven the surviving session still runs the old binding.
        """
        from kiro_crew.providers.base import LLMProvider

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.agent = "oncall"
        slot.model = "oncall-model"
        slot._model_pick_gen = 3
        slot._active_fallback_model = "oncall-fallback"
        slot._fallback_primary_model = "oncall-model"
        slot._fallback_slot_model = "oncall-model"
        slot._fallback_pick_gen = 3
        alive = MagicMock(spec=LLMProvider)
        alive.has_active_turn.return_value = False
        state.sessions.get_provider = MagicMock(return_value=alive)
        state.sessions.reset = AsyncMock(side_effect=RuntimeError("pre-pop boom"))
        self._patch_agent_resolution(monkeypatch)

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/agent", json={"agent": "research"})
            assert resp.status == 500
            assert slot.agent == "oncall"
            assert slot.model == "oncall-model"
            assert slot._model_pick_gen == 3
            assert slot._active_fallback_model == "oncall-fallback"
            assert slot._fallback_primary_model == "oncall-model"
            assert slot._fallback_slot_model == "oncall-model"
            assert slot._fallback_pick_gen == 3

    @pytest.mark.asyncio
    async def test_api_chat_slot_agent_reset_raise_with_successor_succeeds(
        self, tmp_path, monkeypatch
    ):
        """A successor registered post-pop is not the unpopped old session.

        A concurrent send can register a SUCCESSOR session for the same key
        after the pop and before the old session's shutdown raises: the
        helper's probe compares instance IDENTITY, so a different registered
        provider still classifies as post-pop — the switch is committed, the
        successor cold-started from the committed bindings, and the answer is
        200 + warning.
        """
        from kiro_crew.providers.base import LLMProvider

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.agent = "oncall"
        old = MagicMock(spec=LLMProvider)
        old.has_active_turn.return_value = False
        state.sessions.get_provider = MagicMock(return_value=old)

        async def _pop_register_successor_and_raise(*_a, **_k):
            successor = MagicMock(spec=LLMProvider)
            successor.has_active_turn.return_value = False
            state.sessions.get_provider = MagicMock(return_value=successor)
            raise RuntimeError("shutdown boom")

        state.sessions.reset = AsyncMock(side_effect=_pop_register_successor_and_raise)
        self._patch_agent_resolution(monkeypatch)

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/agent", json={"agent": "research"})
            data = await resp.json()
            assert resp.status == 200
            assert data["ok"] is True
            assert data["agent"] == "research"
            assert data["warning"] == "old session teardown incomplete"
            assert slot.agent == "research"

    @pytest.mark.asyncio
    async def test_api_chat_slot_agent_success_commit_spares_concurrent_pick(
        self, tmp_path, monkeypatch
    ):
        """A pick landing during a SUCCESSFUL reset wins over derived values.

        The project/workspace endpoints do not take the slot lock, so a
        user's explicit pick can land while the agent switch's reset await is
        in flight. The commit is compare-and-set against a pre-await
        baseline: the switch's DERIVED workspace/project must not overwrite
        the newer explicit pick, and the response names the slot's final
        reality. `agent` itself still commits — it is owned exclusively by
        this endpoint.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.agent = "oncall"
        slot.workspace = "oncall-ws"
        slot.project = "/tmp/oncall"
        seen_during_reset: list[tuple[str, str]] = []

        async def _concurrent_pick_lands_then_reset_succeeds(*args, **kwargs):
            # The derived bindings must already be committed when the reset
            # runs: a send in this window cold-starts the replacement session
            # from the slot's CURRENT values, and starting the new agent in
            # the old project would run its tools in the wrong repository.
            seen_during_reset.append((slot.workspace, slot.project))
            # A project pick (unlocked endpoint) lands mid-reset.
            slot.project = "/tmp/user-picked"
            return True

        state.sessions.reset = AsyncMock(side_effect=_concurrent_pick_lands_then_reset_succeeds)

        mock_cfg = MagicMock()
        mock_cfg.agents = {"research": MagicMock(workspace="research-ws", memory_store="default")}
        mock_cfg.workspaces = {"research-ws": MagicMock(dir="/tmp/research")}
        mock_cfg.default_workspace = "default"
        mock_cfg.default_memory_store = "default"
        mock_cfg.memory_stores = {}
        mock_cfg.memory = MagicMock()
        mock_bindings = ResolvedBindings(
            workspace_dir=tmp_path,
            memory_store_name="",
            effective_memory_config={},
            kiro_agent="kirocrew",
            selection_kind="template",
        )
        mock_bindings.workspace_dir = Path("/tmp/research")
        mock_bindings.memory_store_name = "default"
        mock_bindings.model = ""
        monkeypatch.setattr("kiro_crew.dashboard.chat.KiroCrewConfig.load", lambda: mock_cfg)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", lambda: mock_cfg
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat.resolve_agent_bindings",
            lambda cfg, name, project_dir=None, **kwargs: mock_bindings,
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.resolve_agent_bindings",
            lambda cfg, name, project_dir=None, **kwargs: mock_bindings,
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat._workspace_name_for_dir",
            lambda cfg, ws_dir: "research-ws",
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._workspace_name_for_dir",
            lambda cfg, ws_dir: "research-ws",
        )

        async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
            resp = await client.post("/api/chat/slots/s1/agent", json={"agent": "research"})
            data = await resp.json()
            assert resp.status == 200
            # The agent itself committed; the derived workspace committed
            # (untouched since baseline); the user's mid-reset project pick
            # SURVIVED the commit instead of being overwritten by the
            # derived project reset.
            assert slot.agent == "research"
            assert slot.workspace == "research-ws"
            assert slot.project == "/tmp/user-picked"
            # The derived workspace was already committed when the reset ran
            # (a replacement session in that window starts under the NEW
            # binding, not the old repository).
            assert seen_during_reset[0][0] == "research-ws"
            # The response names the slot's final reality.
            assert data["workspace"] == "research-ws"

    @pytest.mark.asyncio
    async def test_api_chat_slot_agent_new_agent_visible_during_reset(self, tmp_path, monkeypatch):
        """The new agent is committed before the reset runs.

        A message send landing while the reset await is in flight creates a
        fresh session from the slot's CURRENT bindings — if the agent were
        committed only after the reset, that session would cold-start on the
        OLD agent and stay stale while the switch reports success.
        (`agent` has no unlocked writers, so committing before the reset is
        safe: the failure path's rollback races nobody.)
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.agent = "oncall"
        seen_during_reset: list[str] = []

        async def _observe_then_succeed(*args, **kwargs):
            seen_during_reset.append(slot.agent)
            return True

        state.sessions.reset = AsyncMock(side_effect=_observe_then_succeed)

        def _boom():
            raise RuntimeError("config unreadable")

        # Resolution outcome is irrelevant to the visibility property.
        monkeypatch.setattr("kiro_crew.dashboard.chat.KiroCrewConfig.load", _boom)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", _boom)

        async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
            resp = await client.post("/api/chat/slots/s1/agent", json={"agent": "research"})
            assert resp.status == 200
            assert seen_during_reset == ["research"]
            assert slot.agent == "research"

    @pytest.mark.asyncio
    async def test_api_chat_slot_agent_response_names_workspace_picked_during_persist(
        self, tmp_path, monkeypatch
    ):
        """The response snapshots the workspace LAST, before leaving the lock.

        The metadata persist awaits a worker thread, yielding the event loop
        — a concurrent /workspace pick landing in that window must be what
        the response names, because the acting tab writes the response's
        workspace into its store optimistically. A snapshot taken before the
        persist would hand the tab a stale value that the pick had already
        superseded.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.workspace = "oncall-ws"
        state.sessions.reset = AsyncMock()

        def _boom():
            raise RuntimeError("config unreadable")

        # Resolution outcome is irrelevant; keep the derived commit a no-op.
        monkeypatch.setattr("kiro_crew.dashboard.chat.KiroCrewConfig.load", _boom)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", _boom)

        # A conversation_log whose update_metadata simulates a concurrent
        # /workspace pick landing while the persist thread runs.
        conv = MagicMock()

        def _pick_lands_during_persist(*args, **kwargs):
            slot.workspace = "user-picked-ws"

        conv.update_metadata = MagicMock(side_effect=_pick_lands_during_persist)
        state.conversation_log = conv

        async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
            resp = await client.post("/api/chat/slots/s1/agent", json={"agent": "research"})
            data = await resp.json()
            assert resp.status == 200
            # The response names the newest reality, not a pre-persist
            # snapshot.
            assert data["workspace"] == "user-picked-ws"

    @pytest.mark.asyncio
    async def test_api_chat_slot_agent_updates_project_dir(self, tmp_path, monkeypatch):
        """Switching agent also updates slot.project to the new workspace dir.

        Without this, the dashboard file search stays scoped to the previous
        workspace even after the user selects a different agent.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.project = "/old/project"
        state.sessions.reset = AsyncMock()

        mock_cfg = MagicMock()
        mock_cfg.agents = {"dev": MagicMock(workspace="dev-ws", memory_store="default")}
        mock_cfg.workspaces = {"dev-ws": MagicMock(dir="/workspace/dev")}
        mock_cfg.default_workspace = "default"
        mock_cfg.default_memory_store = "default"
        mock_cfg.memory_stores = {"default": MagicMock()}
        mock_cfg.memory = MagicMock()

        mock_bindings = ResolvedBindings(
            workspace_dir=tmp_path,
            memory_store_name="",
            effective_memory_config={},
            kiro_agent="kirocrew",
            selection_kind="template",
        )
        mock_bindings.workspace_dir = Path("/workspace/dev")
        mock_bindings.memory_store_name = "default"
        mock_bindings.model = ""

        monkeypatch.setattr("kiro_crew.dashboard.chat.KiroCrewConfig.load", lambda: mock_cfg)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", lambda: mock_cfg
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat.resolve_agent_bindings",
            lambda cfg, name, project_dir=None, **kwargs: mock_bindings,
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.resolve_agent_bindings",
            lambda cfg, name, project_dir=None, **kwargs: mock_bindings,
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat._workspace_name_for_dir",
            lambda cfg, ws_dir: "dev-ws",
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._workspace_name_for_dir",
            lambda cfg, ws_dir: "dev-ws",
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.default_project_dir",
            lambda ws: "/workspace/dev",
        )

        async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
            resp = await client.post("/api/chat/slots/s1/agent", json={"agent": "dev"})
            assert resp.status == 200
            assert slot.project == "/workspace/dev"

    @pytest.mark.asyncio
    async def test_api_chat_slot_agent_keeps_project_for_project_agent(self, tmp_path, monkeypatch):
        """Selecting a PROJECT-scope agent must not reset slot.project.

        kiro-cli resolves --agent against $PWD/.kiro/agents, so clobbering the
        project here makes the just-selected agent unresolvable on the next
        turn: the slot advertises it while the default answers — the
        silent-substitution bug this exists to remove.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.project = str(tmp_path / "repo")
        state.sessions.reset = AsyncMock()

        mock_cfg = MagicMock()
        mock_cfg.agents = {}  # not an alias — resolvable only via the project scope

        mock_bindings = ResolvedBindings(
            workspace_dir=tmp_path,
            memory_store_name="",
            effective_memory_config={},
            kiro_agent="kirocrew",
            selection_kind="template",
        )
        mock_bindings.workspace_dir = Path("/workspace/default")
        mock_bindings.requested_resolved = True

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", lambda: mock_cfg
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.resolve_agent_bindings",
            lambda cfg, name, project_dir=None, **kwargs: mock_bindings,
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._workspace_name_for_dir",
            lambda cfg, ws_dir: "default",
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.warm_project_agent_names", AsyncMock()
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.cached_project_agent_names",
            lambda project_dir: frozenset({"repo-bot"}),
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.default_project_dir",
            lambda ws: "/workspace/default",
        )

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/agent", json={"agent": "repo-bot"})
            assert resp.status == 200
            assert slot.project == str(
                tmp_path / "repo"
            ), f"project agent selection clobbered slot.project: {slot.project!r}"

    @pytest.mark.asyncio
    async def test_api_chat_slot_workspace_updates_project_dir(self, tmp_path, monkeypatch):
        """Switching workspace also updates slot.project to the new workspace dir."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.project = "/old/project"
        state.sessions.reset = AsyncMock()

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.default_project_dir",
            lambda ws: "/workspace/new-ws",
        )

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/workspace", json={"workspace": "new-ws"})
            assert resp.status == 200
            assert slot.project == "/workspace/new-ws"

    @pytest.mark.asyncio
    async def test_api_chat_slot_agent_resets_fallback_and_persists_bindings(
        self, tmp_path, monkeypatch
    ):
        """Switching agents resets provider-specific state and persists both bindings.

        The pin is KEPT (the read side withholds it from a provider that does
        not serve it); the fallback walk, which nothing re-scopes at read, is
        reset. Without the persistence, a session resumed after a gateway
        restart reverts to whatever agent (if any) was recorded in the initial
        metadata line.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.agent = "old-agent"
        slot.model = "old-provider-model"
        slot._model_pick_gen = 4
        slot._fallback_candidate_idx = 2
        slot._fallback_walked = ["old-fallback"]
        slot._active_fallback_model = "old-fallback"
        slot._fallback_primary_model = "old-provider-model"
        slot._fallback_slot_model = "old-provider-model"
        slot._fallback_pick_gen = 4
        state.sessions.reset = AsyncMock()

        # Seed a session file so update_metadata has something to patch.
        # Use the canonical colon-separated key that the API handler derives
        # via _history_key_for("s1") → "dashboard:s1".  Using "dashboard_s1"
        # (underscore) maps to the same *file* on disk (_safe_key converts
        # both to "dashboard_s1.jsonl") but creates a different *cache key*,
        # so update_metadata's cache invalidation for "dashboard:s1" would
        # leave the "dashboard_s1" cache entry stale.
        history_key = "dashboard:s1"
        state.conversation_log.append(history_key, "user", "hi", agent="old-agent")
        state.conversation_log.update_metadata(history_key, {"model": "old-provider-model"})
        assert state.conversation_log.get_metadata(history_key).get("agent") == "old-agent"

        # Minimal config stub (agent-binding resolution is exercised by the
        # workspace-focused test above; here we only care about persistence).
        mock_cfg = MagicMock()
        mock_cfg.agents = {}
        monkeypatch.setattr("kiro_crew.dashboard.chat.KiroCrewConfig.load", lambda: mock_cfg)

        async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
            resp = await client.post("/api/chat/slots/s1/agent", json={"agent": "new-agent"})
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["agent"] == "new-agent"

        meta = state.conversation_log.get_metadata(history_key)
        assert (
            meta.get("agent") == "new-agent"
        ), f"expected new-agent in metadata, got {meta.get('agent')!r}"
        assert meta.get("model") == "old-provider-model"
        assert slot.model == "old-provider-model"
        assert slot._model_pick_gen == 4
        assert slot._fallback_candidate_idx == 0
        assert slot._fallback_walked == []
        assert slot._active_fallback_model == ""
        assert slot._fallback_primary_model == ""
        assert slot._fallback_slot_model == ""
        assert slot._fallback_pick_gen == 0

    @pytest.mark.asyncio
    async def test_api_chat_slot_agent_same_agent_keeps_model_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.agent = "same-agent"
        slot.model = "same-provider-model"
        slot._model_pick_gen = 4
        slot._active_fallback_model = "same-fallback"
        slot._fallback_primary_model = "same-provider-model"
        slot._fallback_slot_model = "same-provider-model"
        slot._fallback_pick_gen = 4
        state.sessions.reset = AsyncMock()

        mock_cfg = MagicMock()
        mock_cfg.agents = {}
        monkeypatch.setattr("kiro_crew.dashboard.chat.KiroCrewConfig.load", lambda: mock_cfg)
        # The handler imports the owner check locally from source_providers, so
        # that is the namespace to patch; short-circuiting it keeps this test on
        # the same-agent path instead of the binding-resolution refusal.
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: True,
        )

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/agent", json={"agent": "same-agent"})
            assert resp.status == 200

        assert slot.model == "same-provider-model"
        assert slot._model_pick_gen == 4
        assert slot._active_fallback_model == "same-fallback"
        assert slot._fallback_primary_model == "same-provider-model"
        assert slot._fallback_slot_model == "same-provider-model"
        assert slot._fallback_pick_gen == 4

    @pytest.mark.asyncio
    async def test_api_chat_slot_agent_same_name_kind_switch_clears_the_binding(
        self, tmp_path, monkeypatch
    ):
        """A same-name member->template pick is a rebind, not an affirmation.

        The pin is kept (the read side withholds it from the replacement
        provider); only the fallback walk, which nothing re-scopes at read,
        is reset. Only the kind distinguishes the two picks, so the reset
        keys on it as well as the name.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.agent = "same-agent"
        slot.agent_kind = "member"
        slot.model = "member-provider-model"
        slot._model_pick_gen = 4
        slot._active_fallback_model = "member-fallback"
        slot._fallback_primary_model = "member-provider-model"
        slot._fallback_slot_model = "member-provider-model"
        slot._fallback_pick_gen = 4
        state.sessions.reset = AsyncMock()

        mock_cfg = MagicMock()
        mock_cfg.agents = {}
        monkeypatch.setattr("kiro_crew.dashboard.chat.KiroCrewConfig.load", lambda: mock_cfg)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: True,
        )
        mock_bindings = ResolvedBindings(
            workspace_dir=tmp_path,
            memory_store_name="",
            effective_memory_config={},
            kiro_agent="kirocrew",
            selection_kind="template",
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.resolve_agent_bindings",
            lambda cfg, name, project_dir=None, **kwargs: mock_bindings,
        )

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/agent", json={"agent": "same-agent", "agent_kind": "template"}
            )
            assert resp.status == 200

        assert slot.agent_kind == "template"
        assert slot.model == "member-provider-model"
        assert slot._model_pick_gen == 4
        assert slot._active_fallback_model == ""
        assert slot._fallback_primary_model == ""
        assert slot._fallback_slot_model == ""
        assert slot._fallback_pick_gen == 0

    @pytest.mark.asyncio
    async def test_rolled_back_owner_switch_persists_the_restored_model(
        self, tmp_path, monkeypatch
    ):
        """A refused agent switch preserves the pin in memory and metadata."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.agent = "oncall"
        slot.model = "pinned-model"
        slot._active_fallback_model = "old-fallback"
        slot.workspace = "oncall-ws"
        slot.project = "/tmp/oncall"
        state.conversation_log.append("dashboard:s1", "user", "hi", agent="oncall")
        state.conversation_log.update_metadata("dashboard:s1", {"model": "pinned-model"})

        state.sessions.reset = AsyncMock()
        state.sessions.get_provider = MagicMock(return_value=None)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._record_explicit_agent_selection",
            AsyncMock(side_effect=ValueError("member memory write failed")),
        )
        self._patch_agent_resolution(monkeypatch)

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/agent", json={"agent": "research"})
            assert resp.status != 200

        assert slot.agent == "oncall"
        assert slot.model == "pinned-model"
        metadata = state.conversation_log.get_metadata("dashboard:s1")
        assert metadata.get("agent") == "oncall"
        assert metadata.get("model") == "pinned-model"

    @pytest.mark.asyncio
    async def test_api_chat_slot_create_response_includes_workspace(self, tmp_path, monkeypatch):
        """api_chat_slot_create response includes resolved workspace field.

        Requirements: 2.4
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        mock_cfg = MagicMock()
        mock_cfg.agents = {"research": MagicMock(workspace="research-ws", memory_store="default")}
        mock_cfg.workspaces = {"research-ws": MagicMock(dir="/tmp/research")}
        mock_cfg.default_workspace = "default"
        mock_cfg.default_memory_store = "default"
        mock_cfg.memory_stores = {}
        mock_cfg.memory = MagicMock()

        mock_bindings = ResolvedBindings(
            workspace_dir=tmp_path,
            memory_store_name="",
            effective_memory_config={},
            kiro_agent="kirocrew",
            selection_kind="template",
        )
        mock_bindings.workspace_dir = Path("/tmp/research")
        mock_bindings.memory_store_name = "default"
        mock_bindings.model = ""

        monkeypatch.setattr("kiro_crew.dashboard.chat.KiroCrewConfig.load", lambda: mock_cfg)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", lambda: mock_cfg
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat.resolve_agent_bindings",
            lambda cfg, name, project_dir=None, **kwargs: mock_bindings,
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.resolve_agent_bindings",
            lambda cfg, name, project_dir=None, **kwargs: mock_bindings,
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat._workspace_name_for_dir",
            lambda cfg, ws_dir: "research-ws",
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._workspace_name_for_dir",
            lambda cfg, ws_dir: "research-ws",
        )

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post(
                "/api/chat/slots",
                json={"name": "new-slot", "agent": "research"},
            )
            data = await resp.json()
            assert resp.status == 200
            assert data["workspace"] == "research-ws"

    def test_get_or_create_slot_accepts_workspace_parameter(self, tmp_path, monkeypatch):
        """get_or_create_slot accepts workspace parameter and sets it on the slot.

        Requirements: 2.3
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        slot = state.get_or_create_slot("ws-test", agent="oncall", workspace="oncall-ws")
        assert slot.workspace == "oncall-ws"
        assert slot.agent == "oncall"

        # Default workspace when not specified
        slot2 = state.get_or_create_slot("ws-default")
        assert slot2.workspace == "default"

        # Mode parameter
        slot3 = state.get_or_create_slot("mode-test", mode="design-critique")
        assert slot3.mode == "design-critique"
        assert state.get_or_create_slot("ws-default").mode == ""
