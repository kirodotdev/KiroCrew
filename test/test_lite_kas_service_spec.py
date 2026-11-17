"""``kirocrew-lite`` on KAS runs on Crew's own copy (``config.paths.kas_service_spec_dir``).

A sandbox launcher can write its own MCP server into every spec under
``~/.kiro/agents/`` on each kiro-cli start, after Crew's pre-spawn repair. On KAS,
Crew reads the spec and sends it, so it reads its own copy outside that folder,
and the zero-tool checks read the same copy. kiro-cli's own ``--agent`` route still
loads ``~/.kiro/agents/kirocrew-lite.json``, so there the checks read that file as
before and still refuse an injected server.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from kiro_crew.acp.types import ACP_BACKEND_KAS

_LITE = "kirocrew-lite"
_INJECTED = {
    "tools": ["@creds-agent"],
    "allowedTools": ["@creds-agent"],
    "mcpServers": {"creds-agent": {"command": "creds", "args": ["mcp", "start-server"]}},
}


def _install(monkeypatch) -> tuple[Path, Path]:
    """Install the lite spec; return the user-level file and the KAS copy."""
    from kiro_crew import agent as agent_module
    from kiro_crew.agent_materialization import service_agents

    monkeypatch.setattr(agent_module, "_background_agent_model", lambda: "auto")
    monkeypatch.setattr(agent_module, "_background_cc_model", lambda: "auto")
    monkeypatch.setattr(agent_module.agent_state, "set_cc_model", lambda *_a, **_k: None)
    agent_module.kiro_agents_dir_path().mkdir(parents=True, exist_ok=True)
    from kiro_crew.config import paths

    service_agents._install_lite_agent_fallback()
    user_level = agent_module.kiro_agents_dir_path() / agent_module._LITE_AGENT_FILENAME
    # Spelled out rather than asked of the installer, so this file runs on a tree
    # without the copy and shows what it changes.
    return user_level, paths.mate_agents_dir().parent / "service" / user_level.name


def _inject(path: Path) -> None:
    spec = json.loads(path.read_text())
    path.write_text(json.dumps({**spec, **_INJECTED}))


class _Sessions:
    def __init__(self, provider: Any, on_spawn: Any = None, spawn_backend: Any = None) -> None:
        self.provider = provider
        self.on_spawn = on_spawn
        self.spawn_backend = spawn_backend
        self.calls: list[str] = []

    def _configured_bg_backend_raw(self) -> Any:
        return self.spawn_backend

    async def get_or_create(self, _key: str, **_kw: Any):
        self.calls.append("get_or_create")
        if self.on_spawn is not None:
            self.on_spawn()
        return self.provider, True, False

    def get_agent(self, _key: str) -> str:
        return _LITE

    def get_provider(self, _key: str) -> Any:
        return self.provider

    async def reset(self, _key: str, **_kw: Any) -> None:
        self.calls.append("reset")

    def release(self, _key: str) -> None:
        self.calls.append("release")

    async def recycle_background(self, **_kw: Any) -> None:
        self.calls.append("recycle")


def _provider(backend: str) -> SimpleNamespace:
    from kiro_crew.acp.client import AcpClient

    client = AcpClient(agent=_LITE, acp_backend=backend)
    client._native_skill_projection = None
    return SimpleNamespace(client=client, last_prompt_stats=None)


async def _turn(sessions: _Sessions) -> bool:
    from kiro_crew.llm_helpers import background_turn

    with patch("kiro_crew.dashboard.handlers.usage.persist_token_record_async"):
        async with background_turn(sessions, task="consolidation", agent=_LITE):
            return True


@pytest.fixture(autouse=True)
def _fresh_rewrite_state(_floor_monkeypatch):
    from kiro_crew import llm_helpers

    _floor_monkeypatch.setattr(llm_helpers, "_lite_rewrites", {"begun": 0, "running": 0})


class TestTheCopy:
    def test_the_installer_writes_the_kas_copy_beside_the_user_level_one(self, monkeypatch):
        user_level, service = _install(monkeypatch)

        assert service.parent != user_level.parent
        assert json.loads(service.read_text()) == json.loads(user_level.read_text())

    def test_a_repair_puts_the_kas_copy_back(self, monkeypatch):
        from kiro_crew.agent import repair_lite_agent_spec

        user_level, service = _install(monkeypatch)
        clean = json.loads(service.read_text())
        _inject(service)

        assert repair_lite_agent_spec() is True
        assert json.loads(service.read_text()) == clean

    def test_a_copy_that_cannot_be_resolved_does_not_stop_the_repair(self, monkeypatch):
        """The user-level file is still repaired, nothing raises, and a tooled copy is
        left as it was, so the check after the repair still refuses it."""
        from kiro_crew.agent import repair_lite_agent_spec
        from kiro_crew.agent_materialization import service_agents
        from kiro_crew.agent_sdk.agent_specs import authored_spec_toolless_verdict

        user_level, service = _install(monkeypatch)
        clean = json.loads(user_level.read_text())
        _inject(user_level)
        _inject(service)

        def unresolved() -> Path:
            raise RuntimeError("kirocrew-lite has no KAS service folder")

        monkeypatch.setattr(service_agents, "_lite_service_path", unresolved)

        assert repair_lite_agent_spec() is True
        assert json.loads(user_level.read_text()) == clean
        assert "creds-agent" in json.loads(service.read_text())["mcpServers"]
        assert authored_spec_toolless_verdict(_LITE, backend=ACP_BACKEND_KAS) is False


class TestThePreSpawnVerdictOnKas:
    def test_a_tooled_copy_is_refused(self, monkeypatch):
        from kiro_crew.agent_sdk.agent_specs import authored_spec_toolless_verdict

        _user_level, service = _install(monkeypatch)
        _inject(service)

        assert authored_spec_toolless_verdict(_LITE, backend=ACP_BACKEND_KAS) is False

    def test_a_clean_copy_decides_even_when_the_user_level_file_carries_a_server(self, monkeypatch):
        from kiro_crew.agent_sdk.agent_specs import authored_spec_toolless_verdict

        user_level, _service = _install(monkeypatch)
        _inject(user_level)

        assert authored_spec_toolless_verdict(_LITE, backend=ACP_BACKEND_KAS) is True


class TestTheKasProjection:
    def test_kas_is_sent_the_copy_when_the_user_level_file_carries_a_server(self, monkeypatch):
        from kiro_crew.acp.harness.kas import resolve_projected_spec
        from kiro_crew.agent import kiro_agents_dir_path

        user_level, service = _install(monkeypatch)
        _inject(user_level)

        spec, prompt_dir = resolve_projected_spec(kiro_agents_dir_path(), _LITE, None)

        assert spec.get("mcpServers") == {}
        assert prompt_dir == service.parent

    def test_without_the_copy_the_user_level_file_is_sent(self, monkeypatch):
        from kiro_crew.acp.harness.kas import resolve_projected_spec
        from kiro_crew.agent import kiro_agents_dir_path

        user_level, service = _install(monkeypatch)
        service.unlink(missing_ok=True)
        _inject(user_level)

        spec, _dir = resolve_projected_spec(kiro_agents_dir_path(), _LITE, None)

        assert "creds-agent" in spec.get("mcpServers", {})

    def test_a_broker_stub_picked_from_the_user_level_file_is_withdrawn(
        self, monkeypatch, tmp_path
    ):
        """The session-level stubs are looked up by name from the user-level spec,
        which KAS is not sent: a stub the copy does not mount is withdrawn."""
        from kiro_crew.acp.harness.kas import KasHarness
        from kiro_crew.mcp_gateway import session_servers as session_servers_mod

        user_level, _service = _install(monkeypatch)
        _inject(user_level)
        monkeypatch.setattr(
            session_servers_mod,
            "injection_server_names",
            lambda _overlay, _agent, **_kw: frozenset({"creds-agent"}),
        )

        extras = asyncio.run(
            KasHarness().session_extras(_LITE, work_dir=str(tmp_path), mcp_gateway_overlay=object())
        )

        assert extras.withdrawn_stub_names == frozenset({"creds-agent"})

    def test_the_stubs_session_new_carries_are_the_ones_withdrawn(self, monkeypatch, tmp_path):
        """The captured stub names decide, even when the overlay read during the
        projection names none of them."""
        from kiro_crew.acp.harness.kas import KasHarness
        from kiro_crew.mcp_gateway import session_servers as session_servers_mod

        _install(monkeypatch)
        monkeypatch.setattr(
            session_servers_mod, "injection_server_names", lambda *_a, **_kw: frozenset()
        )

        extras = asyncio.run(
            KasHarness().session_extras(
                _LITE,
                work_dir=str(tmp_path),
                mcp_gateway_overlay=object(),
                worker_stub_names=frozenset({"captured"}),
            )
        )

        assert extras.withdrawn_stub_names == frozenset({"captured"})

    def test_the_runtime_captures_lites_stub_names_on_kas_only(self):
        from kiro_crew.acp.runtime_session_setup import _kas_worker_stub_names

        pooled = [{"name": "srv"}]

        assert _kas_worker_stub_names(ACP_BACKEND_KAS, _LITE, "", pooled) == frozenset({"srv"})
        assert _kas_worker_stub_names("", _LITE, "", pooled) is None
        assert _kas_worker_stub_names(ACP_BACKEND_KAS, "kirocrew", "k", pooled) is None


class TestABackgroundTurn:
    def test_on_kas_a_server_written_during_the_start_does_not_refuse_it(self, monkeypatch):
        """The consolidation turn gets its session although the user-level file
        mounts a server again once the start runs."""
        user_level, _service = _install(monkeypatch)
        sessions = _Sessions(_provider(ACP_BACKEND_KAS), on_spawn=lambda: _inject(user_level))

        assert asyncio.run(_turn(sessions)) is True
        assert "reset" not in sessions.calls

    def test_on_kas_a_server_written_into_the_copy_still_refuses(self, monkeypatch):
        _user_level, service = _install(monkeypatch)
        sessions = _Sessions(_provider(ACP_BACKEND_KAS), on_spawn=lambda: _inject(service))

        with pytest.raises(RuntimeError, match="not acquired"):
            asyncio.run(_turn(sessions))
        assert sessions.calls == ["get_or_create", "reset", "release"]

    def test_on_kiro_cli_a_server_written_during_the_start_still_refuses(self, monkeypatch):
        """kiro-cli loads ``~/.kiro/agents/kirocrew-lite.json`` itself: unchanged."""
        user_level, _service = _install(monkeypatch)
        sessions = _Sessions(_provider(""), on_spawn=lambda: _inject(user_level))

        with pytest.raises(RuntimeError, match="not acquired"):
            asyncio.run(_turn(sessions))
        assert sessions.calls == ["get_or_create", "reset", "release"]

    def test_a_tooled_copy_is_refused_before_the_start(self, monkeypatch):
        from kiro_crew.agent_materialization import auto_approve

        _user_level, service = _install(monkeypatch)
        _inject(service)

        def refuse(*_a: Any, **_k: Any) -> None:
            from kiro_crew import agent as agent_module

            raise agent_module.SharedAgentHomeRefused("declined")

        monkeypatch.setattr(auto_approve, "write_governed_spec", refuse)
        sessions = _Sessions(_provider(ACP_BACKEND_KAS))

        with pytest.raises(RuntimeError, match="not acquired"):
            asyncio.run(_turn(sessions))
        assert sessions.calls == []

    def test_on_kas_a_server_already_in_the_user_level_file_needs_no_repair(self, monkeypatch):
        """The launcher rewrote the user-level file on an earlier start. KAS is sent
        the copy, so the turn runs with no repair and no reset."""
        from kiro_crew.llm_helpers_parts import background_turns

        user_level, _service = _install(monkeypatch)
        _inject(user_level)
        repairs: list[int] = []
        monkeypatch.setattr(
            background_turns, "_rewrite_lite_spec", lambda _s: repairs.append(1) or _noop()
        )
        sessions = _Sessions(_provider(ACP_BACKEND_KAS), spawn_backend=ACP_BACKEND_KAS)

        assert asyncio.run(_turn(sessions)) is True
        assert repairs == []
        assert "reset" not in sessions.calls

    def test_on_kiro_cli_a_tooled_copy_it_never_loads_does_not_refuse(self, monkeypatch):
        _user_level, service = _install(monkeypatch)
        _inject(service)
        sessions = _Sessions(_provider(""), spawn_backend="")

        assert asyncio.run(_turn(sessions)) is True
        assert "reset" not in sessions.calls

    def test_a_read_taken_for_kas_does_not_vouch_for_a_kiro_cli_start(self, monkeypatch):
        """The read before the start judged the copy; a kiro-cli start loads the
        user-level file, which that read never looked at."""
        user_level, _service = _install(monkeypatch)
        clean = user_level.read_text()
        _inject(user_level)
        sessions = _Sessions(
            _provider(""),
            on_spawn=lambda: user_level.write_text(clean),
            spawn_backend=ACP_BACKEND_KAS,
        )

        with pytest.raises(RuntimeError, match="not acquired"):
            asyncio.run(_turn(sessions))
        assert sessions.calls == ["get_or_create", "reset", "release"]


async def _noop() -> None:
    return None


class TestTheCopyIsProtectedLikeTheFileItStandsIn:
    """The copy is the spec KAS lite sessions run, so it keeps the user-level file's
    two guards: the file-edit tool gate and the sandbox's read-only seal."""

    def test_the_literal_is_the_home_relative_tail_of_the_folder(self, tmp_path, monkeypatch):
        from kiro_crew import security
        from kiro_crew.config import paths

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
        monkeypatch.delenv("KIRO_HOME", raising=False)
        monkeypatch.setattr(paths, "_mate_agents_dir_override", None)
        folder = paths.kas_service_spec_dir(_LITE)

        assert folder is not None
        assert security.paths._KIRO_MATE_SERVICE_DIR == folder.relative_to(tmp_path).as_posix()

    def test_a_file_edit_write_into_the_folder_is_refused(self):
        from kiro_crew.security import is_sensitive_path, is_sensitive_write_path

        assert is_sensitive_write_path("~/.kiro/mates/service/kirocrew-lite.json") is True
        assert is_sensitive_write_path("~/.kiro/mates/service") is True
        assert is_sensitive_path("~/.kiro/mates/service/kirocrew-lite.json") is False
        assert is_sensitive_write_path("~/.kiro/mates/service-old/x.json") is False

    def test_the_sandbox_seals_the_folder_and_creates_it_when_absent(self):
        import os

        from kiro_crew import sandbox
        from kiro_crew.config.paths import kas_service_spec_dir

        folder = os.path.normpath(str(kas_service_spec_dir(_LITE)))
        os.makedirs(os.path.dirname(folder), exist_ok=True)

        assert folder in sandbox._resolved_kiro_agents_targets()
        dirs, _files = sandbox._sealable_absent_ceilings()
        assert folder in [os.path.normpath(d) for d in dirs]
