"""``agent.session_recap``: the opt-in that makes KAS send a session recap.

KAS generates a recap only when the initialize request's
``clientCapabilities._meta.kiro.settings`` carries ``sessionRecap.enabled``. With
it on, KAS sends one after every turn and replays the latest one inside the
``session/load`` window. These tests pin the config-to-wire path (config field,
loader, factory, provider, both runtime constructions) and the accessor the
dashboard reads after a resume prefetch. The handshake bytes themselves are
pinned end to end against a stub agent in ``test_kas_spawn.py``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp import kas_wire
from kiro_crew.acp.types import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.providers.base import LLMProvider
from kiro_crew.testing.ids import unallocatable_pids

# ── the wire entry ──


def test_the_setting_entry_is_the_key_kas_reads():
    assert kas_wire.session_recap_settings(True) == {"sessionRecap": {"enabled": True}}


def test_off_sends_nothing_so_the_handshake_is_unchanged():
    assert kas_wire.session_recap_settings(False) == {}


# ── config ──


def test_the_setting_is_off_by_default():
    assert KiroCrewConfig().agent.session_recap is False


def _load_agent_config(agent_data: dict, tmp_path: Path) -> KiroCrewConfig:
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"agent": agent_data}), encoding="utf-8")
    with patch("kiro_crew.config.loader.config_path", return_value=cfg_file):
        return KiroCrewConfig.load()


@pytest.mark.parametrize("value", [True, False])
def test_the_loader_reads_the_setting(value, tmp_path):
    cfg = _load_agent_config({"session_recap": value}, tmp_path)
    assert cfg.agent.session_recap is value
    assert cfg.to_dict()["agent"]["session_recap"] is value


@pytest.mark.parametrize("value", [True, False])
def test_the_factory_hands_the_setting_to_the_provider(value):
    cfg = KiroCrewConfig()
    cfg.agent.acp_backend = ACP_BACKEND_KAS
    cfg.agent.session_recap = value
    provider = cfg.create_provider_factory()(session_key="dashboard:chat-1", agent="")
    assert isinstance(provider, AcpProvider)
    assert provider._session_recap is value


# ── the provider hands it to every runtime it spawns ──


def _build_provider(backend: str, **kwargs) -> AcpProvider:
    with patch("kiro_crew.providers.acp.AcpClient"):
        provider = AcpProvider(acp_backend=backend, **kwargs)
    provider._client = MagicMock()
    provider._client.backend = backend
    return provider


@pytest.mark.parametrize("value", [True, False])
def test_the_first_runtime_is_built_with_the_setting(value, monkeypatch):
    captured: dict = {}

    class _Stop(Exception):
        pass

    class _Runtime:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            raise _Stop

    monkeypatch.setattr("kiro_crew.providers.acp.AcpRuntime", _Runtime)
    provider = _build_provider(ACP_BACKEND_KAS, session_recap=value)
    provider._client._work_dir = MagicMock()
    provider._client._agent = "kirocrew"
    provider._client._resume_session_id = ""

    async def run():
        with pytest.raises(_Stop):
            await provider._start_kiro_runtime_impl({}, {})

    asyncio.run(run())
    assert captured.get("session_recap") is value


def test_the_resume_respawn_carries_the_setting_too():
    """A runtime that dies during resume is respawned WITH the opt-in: the
    setting travels only through this constructor argument, so a respawn
    without it would load the session with no recap for its whole life."""
    provider = _build_provider(ACP_BACKEND_KAS, session_recap=True)
    provider._client._work_dir = "/tmp/ws"
    provider._client._agent = "kirocrew"
    provider._client._sandbox_mode = "auto"
    provider._client._extra_env = {}
    provider._client._mcp_gateway_overlay = None
    provider._client._mcp_gateway_socket = None
    provider._client._model = "auto"
    provider._client._resume_session_id = "old-sess"

    dead_pid, fresh_pid = unallocatable_pids(2)
    dead = MagicMock()
    dead.pid = dead_pid
    dead.spawn = AsyncMock()
    dead.is_alive = MagicMock(return_value=False)
    dead.kill = AsyncMock()
    dead.load_session = AsyncMock(side_effect=RuntimeError("load failed"))
    dead.saw_not_logged_in = MagicMock(return_value=False)
    fresh_handle = MagicMock()
    fresh_handle.session_id = "fresh"
    fresh_handle.set_model = AsyncMock()
    fresh_handle.store_session_config = MagicMock()
    fresh = MagicMock()
    fresh.pid = fresh_pid
    fresh.spawn = AsyncMock()
    fresh.is_alive = MagicMock(return_value=True)
    fresh.create_session = AsyncMock(return_value=fresh_handle)
    fresh.saw_not_logged_in = MagicMock(return_value=False)
    runtimes = iter([dead, fresh])
    constructions: list[dict] = []

    def build(**kw):
        constructions.append(kw)
        return next(runtimes)

    with (
        patch("kiro_crew.providers.acp.AcpRuntime", side_effect=build),
        patch(
            "kiro_crew.providers.acp.AcpSessionProvider",
            side_effect=lambda handle, runtime, **kw: MagicMock(
                _handle=handle, _runtime=runtime, resumed=False
            ),
        ),
        patch("pathlib.Path.exists", return_value=True),
    ):
        asyncio.run(provider._start_kiro_runtime())

    assert len(constructions) == 2, "the resume fallback did not respawn"
    assert [c.get("session_recap") for c in constructions] == [True, True]


# ── the accessor the dashboard reads after a resume prefetch ──


def test_the_accessor_is_declared_on_the_provider_contract():
    """H14: declared on ``LLMProvider`` with a safe default, never probed for a
    private name by the dashboard."""
    assert "take_session_recap" in vars(LLMProvider)
    assert LLMProvider.take_session_recap(MagicMock()) is None


def test_the_placeholder_client_has_no_recap():
    provider = _build_provider(ACP_BACKEND_KAS, session_recap=True)
    provider._client = object()
    assert provider.take_session_recap() is None


def test_the_provider_returns_what_the_session_handle_parked():
    from kiro_crew.acp.session_provider import AcpSessionProvider

    handle = MagicMock()
    handle.take_session_recap = MagicMock(return_value="Goal: X. Next: Y.")
    session_provider = AcpSessionProvider.__new__(AcpSessionProvider)
    session_provider._handle = handle
    provider = _build_provider(ACP_BACKEND_KAS, session_recap=True)
    provider._client = session_provider

    assert provider.take_session_recap() == "Goal: X. Next: Y."
    handle.take_session_recap.assert_called_once_with()


@pytest.mark.parametrize("parked", [None, "", 7])
def test_no_text_reads_as_no_recap(parked):
    from kiro_crew.acp.session_provider import AcpSessionProvider

    handle = MagicMock()
    handle.take_session_recap = MagicMock(return_value=parked)
    session_provider = AcpSessionProvider.__new__(AcpSessionProvider)
    session_provider._handle = handle
    provider = _build_provider(ACP_BACKEND_KIRO)
    provider._client = session_provider

    assert provider.take_session_recap() is None
