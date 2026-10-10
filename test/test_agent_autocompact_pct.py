"""Per-agent default auto-compact threshold (``agents.<name>.autocompact_pct``).

Precedence, highest first: the session's own override (the context popover
slider), the default the session's agent declares, the global
``session.autocompact_pct``. Pinned at each layer the defect would live in:

- the loader, which must keep a hand-edited value inside the global's range and
  read junk as "inherit";
- the compaction gate, which must fire at the agent's number for that agent's
  sessions only, and still yield to a session override;
- the dashboard endpoint, which must tell the popover which default is in force.
"""

from __future__ import annotations

import json
import tempfile
import unittest.mock
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config.loader import (
    AUTOCOMPACT_PCT_MAX,
    AUTOCOMPACT_PCT_MIN,
    KiroCrewAgentConfig,
    KiroCrewConfig,
    crew_autocompact_pct,
)
from kiro_crew.dashboard.chat import api_chat_slot_autocompact
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.session import SessionManager, _Session


def _load_agents(agents: dict) -> KiroCrewConfig:
    """Load a config file holding *agents* through the real ``load()`` path."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"agents": agents}, f)
        tmp = Path(f.name)
    try:
        with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
            return KiroCrewConfig.load()
    finally:
        tmp.unlink(missing_ok=True)


class TestLoader:
    def test_a_declared_value_survives_load(self) -> None:
        cfg = _load_agents({"orchestrator": {"kiro_agent": "kirocrew", "autocompact_pct": 45}})
        assert cfg.agents["orchestrator"].autocompact_pct == 45.0

    def test_an_absent_value_inherits(self) -> None:
        cfg = _load_agents({"worker": {"kiro_agent": "kirocrew"}})
        assert cfg.agents["worker"].autocompact_pct == 0.0
        assert crew_autocompact_pct(cfg, "worker") is None

    def test_out_of_range_values_clamp_to_the_global_range(self) -> None:
        cfg = _load_agents(
            {
                "low": {"kiro_agent": "kirocrew", "autocompact_pct": 1},
                "high": {"kiro_agent": "kirocrew", "autocompact_pct": 99},
            }
        )
        assert cfg.agents["low"].autocompact_pct == AUTOCOMPACT_PCT_MIN
        assert cfg.agents["high"].autocompact_pct == AUTOCOMPACT_PCT_MAX

    def test_junk_and_negative_values_inherit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # jsonschema off, so the raw values reach the coercer rather than being
        # stripped by validation first.
        import kiro_crew.config.validation as validation

        monkeypatch.setattr(validation, "_HAS_JSONSCHEMA", False)
        cfg = _load_agents(
            {
                "junk": {"kiro_agent": "kirocrew", "autocompact_pct": "fast"},
                "flag": {"kiro_agent": "kirocrew", "autocompact_pct": True},
                "neg": {"kiro_agent": "kirocrew", "autocompact_pct": -20},
            }
        )
        for name in ("junk", "flag", "neg"):
            assert cfg.agents[name].autocompact_pct == 0.0, name
            assert crew_autocompact_pct(cfg, name) is None, name

    def test_resolver_ignores_unknown_and_empty_names(self) -> None:
        cfg = KiroCrewConfig()
        cfg.agents = {"orch": KiroCrewAgentConfig(kiro_agent="kirocrew", autocompact_pct=40.0)}
        assert crew_autocompact_pct(cfg, "orch") == 40.0
        assert crew_autocompact_pct(cfg, "") is None
        assert crew_autocompact_pct(cfg, "nobody") is None


def _manager(**agents: float) -> SessionManager:
    cfg = KiroCrewConfig()
    cfg.agents = {
        name: KiroCrewAgentConfig(kiro_agent="kirocrew", autocompact_pct=pct)
        for name, pct in agents.items()
    }
    return SessionManager(cfg, provider_factory=lambda *a, **k: object())


def _register(mgr: SessionManager, key: str, member: str) -> None:
    """Place a live session on *key* whose allocation selected crew *member*."""
    session = _Session(provider=MagicMock(), agent="kirocrew")
    session.capability_member = member
    mgr._sessions[mgr._fold_key(key)] = session


class TestPrecedence:
    def test_agent_default_moves_the_gate_for_its_sessions_only(self) -> None:
        mgr = _manager(orchestrator=40.0)
        glob = mgr._cfg.session.autocompact_pct
        _register(mgr, "conductor", "orchestrator")
        _register(mgr, "plain", "")
        reading = 50.0
        assert reading < glob

        # The orchestrator's session compacts at its own, lower number...
        assert mgr.effective_autocompact_pct("conductor") == 40.0
        assert mgr.agent_autocompact_default("conductor") == ("orchestrator", 40.0)
        assert mgr._compaction_gate_decision("conductor", object(), reading) != "below_threshold"
        # ...while a session with no crew at the same usage follows the global.
        assert mgr.effective_autocompact_pct("plain") == glob
        assert mgr._compaction_gate_decision("plain", object(), reading) == "below_threshold"

    def test_a_worker_default_can_sit_above_the_global(self) -> None:
        mgr = _manager(worker=85.0)
        _register(mgr, "w", "worker")
        assert mgr.effective_autocompact_pct("w") == 85.0
        assert mgr._compaction_gate_decision("w", object(), 80.0) == "below_threshold"

    def test_a_session_override_wins_over_the_agent_default(self) -> None:
        mgr = _manager(orchestrator=40.0)
        _register(mgr, "conductor", "orchestrator")
        mgr.set_autocompact_pct("conductor", 75.0)
        assert mgr.effective_autocompact_pct("conductor") == 75.0
        # Clearing the override returns to the agent's default, not the global.
        mgr.set_autocompact_pct("conductor", None)
        assert mgr.effective_autocompact_pct("conductor") == 40.0

    def test_a_crew_that_declares_nothing_follows_the_global(self) -> None:
        mgr = _manager(builder=0.0)
        _register(mgr, "b", "builder")
        assert mgr.agent_autocompact_default("b") is None
        assert mgr.effective_autocompact_pct("b") == mgr._cfg.session.autocompact_pct

    def test_no_live_session_has_no_agent_default(self) -> None:
        mgr = _manager(orchestrator=40.0)
        assert mgr.agent_autocompact_default("absent") is None

    def test_a_config_change_reaches_live_sessions(self) -> None:
        """The manager's config is replaced by the watcher; the next reading uses it."""
        mgr = _manager(orchestrator=40.0)
        _register(mgr, "conductor", "orchestrator")
        changed = KiroCrewConfig()
        changed.agents = {"orchestrator": KiroCrewAgentConfig(autocompact_pct=60.0)}
        mgr._cfg = changed
        assert mgr.effective_autocompact_pct("conductor") == 60.0


def _make_app(state: DashboardState) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/chat/slots/{slot}/autocompact", api_chat_slot_autocompact)
    return app


class TestEndpoint:
    @pytest.mark.asyncio
    async def test_get_reports_the_agent_default_in_force(self) -> None:
        mgr = _manager(orchestrator=40.0)
        slot = _ChatSlot("test")
        state = MagicMock(spec=DashboardState)
        state._slots = {slot.key: slot}
        state.sessions = mgr
        state.conversation_log = MagicMock()
        with unittest.mock.patch(
            "kiro_crew.dashboard.chat_handlers.effective_session_key", return_value="conductor"
        ):
            _register(mgr, "conductor", "orchestrator")
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.get("/api/chat/slots/test/autocompact")
                assert resp.status == 200
                data = await resp.json()
        assert data["pct"] is None
        assert data["agent_pct"] == 40.0
        assert data["agent"] == "orchestrator"

    @pytest.mark.asyncio
    async def test_get_reports_null_when_the_agent_declares_none(self) -> None:
        mgr = _manager()
        slot = _ChatSlot("test")
        state = MagicMock(spec=DashboardState)
        state._slots = {slot.key: slot}
        state.sessions = mgr
        state.conversation_log = MagicMock()
        with unittest.mock.patch.object(KiroCrewConfig, "load", return_value=_crew_cfg()):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.get("/api/chat/slots/test/autocompact")
                data = await resp.json()
        assert data["agent_pct"] is None
        assert data["agent"] is None

    @pytest.mark.asyncio
    async def test_get_without_a_live_session_reports_the_slots_crew(self) -> None:
        """A slot nobody has started yet shows what its first session compacts at."""
        cfg = _crew_cfg(orchestrator=45.0, worker=80.0)
        slot = _ChatSlot("test")
        slot.agent = "orchestrator"
        state = MagicMock(spec=DashboardState)
        state._slots = {slot.key: slot}
        state.sessions = _manager()
        state.conversation_log = MagicMock()
        with unittest.mock.patch.object(KiroCrewConfig, "load", return_value=cfg):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.get("/api/chat/slots/test/autocompact")
                data = await resp.json()
        assert data["agent_pct"] == 45.0
        assert data["agent"] == "orchestrator"

    @pytest.mark.asyncio
    async def test_get_without_a_live_session_follows_the_default_crew(self) -> None:
        """An unbound slot starts under the default crew, so it shows that crew's value."""
        cfg = _crew_cfg(orchestrator=45.0, worker=80.0)
        cfg.default_agent = "worker"
        slot = _ChatSlot("test")
        state = MagicMock(spec=DashboardState)
        state._slots = {slot.key: slot}
        state.sessions = _manager()
        state.conversation_log = MagicMock()
        with unittest.mock.patch.object(KiroCrewConfig, "load", return_value=cfg):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.get("/api/chat/slots/test/autocompact")
                data = await resp.json()
        assert data["agent_pct"] == 80.0
        assert data["agent"] == "worker"

    @pytest.mark.asyncio
    async def test_a_credential_shaped_crew_name_is_redacted_in_the_label(self) -> None:
        """The name is display text: a token-shaped crew name never reaches the browser."""
        token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
        cfg = _crew_cfg(**{token: 45.0})
        slot = _ChatSlot("test")
        slot.agent = token
        state = MagicMock(spec=DashboardState)
        state._slots = {slot.key: slot}
        state.sessions = _manager()
        state.conversation_log = MagicMock()
        with unittest.mock.patch.object(KiroCrewConfig, "load", return_value=cfg):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.get("/api/chat/slots/test/autocompact")
                body = await resp.text()
                data = json.loads(body)
        assert data["agent_pct"] == 45.0
        assert token not in body
        assert data["agent"].startswith("[REDACTED")


def _crew_cfg(**agents: float) -> KiroCrewConfig:
    """A config whose crews declare *agents* (name -> pct), plus a silent default."""
    cfg = KiroCrewConfig()
    cfg.agents = {
        name: KiroCrewAgentConfig(kiro_agent="kirocrew", autocompact_pct=pct)
        for name, pct in {"default": 0.0, **agents}.items()
    }
    cfg.default_agent = "default"
    return cfg


class _ReplProvider:
    """A provider whose context sits at 50%, below the global but above 40%."""

    manual_compact_unsupported_backend = None

    def __init__(self) -> None:
        self.compact = unittest.mock.AsyncMock()
        self.shutdown = unittest.mock.AsyncMock()
        self.start = unittest.mock.AsyncMock()

    def context_usage_pct(self) -> float:
        return 50.0


async def _repl_once(
    monkeypatch: pytest.MonkeyPatch, cfg: KiroCrewConfig, agent: str
) -> _ReplProvider:
    import kiro_crew.cli_chat as cli_chat

    lines = iter(["hello"])

    def fake_input(_prompt: str = "") -> str:
        try:
            return next(lines)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr(cli_chat, "_send_and_print", unittest.mock.AsyncMock())
    provider = _ReplProvider()
    await cli_chat._interactive(provider, cfg, agent=agent)
    return provider


class TestCliChat:
    @pytest.mark.asyncio
    async def test_the_repl_compacts_at_its_crews_number(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _crew_cfg(orchestrator=40.0)
        assert cfg.session.autocompact_pct > 50.0
        provider = await _repl_once(monkeypatch, cfg, "orchestrator")
        provider.start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_the_repl_follows_the_global_for_a_crew_that_declares_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _crew_cfg(builder=0.0)
        provider = await _repl_once(monkeypatch, cfg, "builder")
        provider.start.assert_not_awaited()
