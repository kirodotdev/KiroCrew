"""Fixtures and doubles shared by the ``test_stale_config*.py`` modules.

The provider double, the isolated kiro home, the spec writers, the fingerprint
shorthand and the slot, idle-chat and path-gate harnesses.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import config_staleness as cs
from kiro_crew.dashboard import stale_config
from kiro_crew.dashboard.stale_config import (
    ConfigFingerprint,
    SpawnInputs,
    compute_fingerprint,
    make_record,
)


class _Provider:
    """A weak-referenceable provider double with the attributes the gate reads."""

    #: The ``LLMProvider`` default: no warm-pool receipt.
    pool_spawn_config = None

    def __init__(self, *, hot_reload: bool = False, active: bool = False) -> None:
        self.mcp_config_hot_reload = hot_reload
        self._active = active

    def has_active_turn(self) -> bool:
        return self._active


_A = ConfigFingerprint(reconcilable="r-a", spawn_only="s-a")
_CHANGED = ConfigFingerprint(reconcilable="r-b", spawn_only="s-a")


@pytest.fixture(autouse=True, name="_scan_on_every_os")
def _scan_on_every_os_fixture(_floor_monkeypatch) -> None:
    """Exercise the scanning paths on Windows too; the Windows tests switch it back on."""
    _floor_monkeypatch.setattr(stale_config, "WINDOWS_SCAN_DISABLED", False)


@pytest.fixture(name="kiro_home")
def kiro_home_fixture(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "kiro"
    (home / "agents").mkdir(parents=True)
    (home / "settings").mkdir()
    monkeypatch.setattr("kiro_crew.config.paths.kiro_home", lambda: home)
    monkeypatch.setattr("kiro_crew.agent.kiro_agents_dir_path", lambda: home / "agents")
    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", home / "settings" / "mcp.json")
    return home


def _gate_refuses(monkeypatch, refused) -> None:
    """Make the path gate refuse every raw path *refused* names."""
    real = stale_config.validate_file_path
    monkeypatch.setattr(
        stale_config, "validate_file_path", lambda raw: None if refused(raw) else real(raw)
    )


def _write_spec(home: Path, body: dict) -> None:
    (home / "agents" / "kirocrew.json").write_text(json.dumps({"name": "kirocrew", **body}))


def _write_workspace_spec(project: Path, body: dict) -> None:
    agents = project / ".kiro" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "kirocrew.json").write_text(json.dumps({"name": "kirocrew", **body}))


def _fp(project: str = "", backend: str = "") -> ConfigFingerprint:
    return compute_fingerprint(SpawnInputs(agent="kirocrew", project=project), backend=backend)


_SERVER = {"a": {"command": "a"}}


def _pool_manager(monkeypatch, tmp_path, *, pool_agent: str = "kirocrew"):
    from test_session_pool import _make_manager

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "kirocrew_home"))
    mgr, _factory = _make_manager(pool_size=1, pool_agent=pool_agent)
    return mgr


def _status_slot(tmp_path, monkeypatch, *, hot_reload=False, recorded=None, project=""):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    provider = _Provider(hot_reload=hot_reload)
    state.sessions.get_provider = MagicMock(return_value=provider)
    state.push_slots_update = MagicMock()
    slot._spawn_config = make_record(
        provider, SpawnInputs("kirocrew", project), recorded or _fp(project=project)
    )
    cfg = MagicMock()
    cfg.agent.member_acp_backend = ""
    cfg.agent.acp_backend = ""
    monkeypatch.setattr(cs.KiroCrewConfig, "load", MagicMock(return_value=cfg))
    return state, slot


def _idle_chats(tmp_path, monkeypatch, kiro_home, *, keys=("s1",)):
    """Live, recorded, idle chats on the current config, as a sweep sees them."""
    state = _make_state(tmp_path)
    providers = {}
    for key in keys:
        slot = state.get_or_create_slot(key)
        providers[cs.effective_session_key(slot)] = _Provider()
        slot._spawn_config = make_record(
            providers[cs.effective_session_key(slot)], SpawnInputs("kirocrew", ""), _fp()
        )
    state.sessions.get_provider = MagicMock(side_effect=lambda k: providers.get(k))
    state.push_slots_update = MagicMock()
    cfg = MagicMock()
    cfg.agent.member_acp_backend = ""
    cfg.agent.acp_backend = ""
    monkeypatch.setattr(cs.KiroCrewConfig, "load", MagicMock(return_value=cfg))
    return state
