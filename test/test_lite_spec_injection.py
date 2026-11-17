"""``kirocrew-lite`` on KAS tolerates an injection a registered filter removes exactly.

Some tools write their own MCP server into every spec in the shared agents
directory on each launch. An edition registers a filter for that exact shape
(``kiro_crew.agent_spec_injections``), the same seam the knowledge worker uses. On
KAS, Crew sends the spec itself, so it sends the template instead of the file and
both zero-tool checks judge that spec. kiro-cli loads the file on disk, so there
the injection is still refused, as is anything a filter does not remove exactly.
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
_SERVER = "other-tool"
_ENTRY = {"command": "other-tool", "args": ["serve"]}


def _inject(path: Path, entry: dict | None = None, server: str = _SERVER) -> None:
    spec = json.loads(path.read_text())
    spec["mcpServers"] = {**spec.get("mcpServers", {}), server: entry or dict(_ENTRY)}
    spec["tools"] = [*spec.get("tools", []), f"@{server}"]
    spec["allowedTools"] = [f"@{server}"]
    path.write_text(json.dumps(spec))


def _remove_exact_injection(spec: dict[str, Any]) -> dict[str, Any] | None:
    servers = spec.get("mcpServers")
    if not isinstance(servers, dict) or servers.get(_SERVER) != _ENTRY:
        return None
    if spec.get("allowedTools") != [f"@{_SERVER}"] or f"@{_SERVER}" not in spec.get("tools", []):
        return None
    out = dict(spec)
    out["mcpServers"] = {k: v for k, v in servers.items() if k != _SERVER}
    out["tools"] = [t for t in spec["tools"] if t != f"@{_SERVER}"]
    del out["allowedTools"]
    return out


@pytest.fixture
def registered():
    from kiro_crew import agent_spec_injections

    agent_spec_injections.register_spec_injection_filter("test-tool", _remove_exact_injection)
    yield
    agent_spec_injections.unregister_spec_injection_filter("test-tool")


@pytest.fixture(autouse=True)
def _fresh_rewrite_state(_floor_monkeypatch):
    from kiro_crew import llm_helpers

    _floor_monkeypatch.setattr(llm_helpers, "_lite_rewrites", {"begun": 0, "running": 0})


def _install(monkeypatch) -> Path:
    from kiro_crew import agent as agent_module
    from kiro_crew.agent_materialization import service_agents

    monkeypatch.setattr(agent_module, "_background_agent_model", lambda: "auto")
    monkeypatch.setattr(agent_module, "_background_cc_model", lambda: "auto")
    monkeypatch.setattr(agent_module.agent_state, "set_cc_model", lambda *_a, **_k: None)
    agent_module.kiro_agents_dir_path().mkdir(parents=True, exist_ok=True)
    service_agents._install_lite_agent_fallback()
    return agent_module.kiro_agents_dir_path() / agent_module._LITE_AGENT_FILENAME


class _Sessions:
    def __init__(self, provider: Any, backend: Any, on_spawn: Any = None) -> None:
        self.provider = provider
        self.backend = backend
        self.on_spawn = on_spawn
        self.calls: list[str] = []

    def _configured_bg_backend_raw(self) -> Any:
        return self.backend

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


def _sessions(backend: str, on_spawn: Any = None) -> _Sessions:
    return _Sessions(_provider(backend), backend, on_spawn)


async def _turn(sessions: _Sessions) -> bool:
    from kiro_crew.llm_helpers import background_turn

    with patch("kiro_crew.dashboard.handlers.usage.persist_token_record_async"):
        async with background_turn(sessions, task="consolidation", agent=_LITE):
            return True


class TestABackgroundTurnOnKas:
    def test_a_registered_injection_does_not_refuse_it(self, monkeypatch, registered):
        """The file carries the injection before the turn and gets it again during
        the start; the turn runs without a repair or a reset."""
        from kiro_crew.llm_helpers_parts import background_turns

        path = _install(monkeypatch)
        _inject(path)
        injected = path.read_text()
        repairs: list[int] = []

        async def no_repair(_sessions: Any) -> None:
            repairs.append(1)

        monkeypatch.setattr(background_turns, "_rewrite_lite_spec", no_repair)
        sessions = _sessions(ACP_BACKEND_KAS, on_spawn=lambda: path.write_text(injected))

        assert asyncio.run(_turn(sessions)) is True
        assert repairs == []
        assert "reset" not in sessions.calls
        assert "@other-tool" in json.loads(path.read_text())["tools"]

    def test_an_injection_no_filter_removes_exactly_still_refuses(self, monkeypatch, registered):
        path = _install(monkeypatch)
        sessions = _sessions(
            ACP_BACKEND_KAS,
            on_spawn=lambda: _inject(path, entry={**_ENTRY, "env": {"EXTRA": "1"}}),
        )

        with pytest.raises(RuntimeError, match="not acquired"):
            asyncio.run(_turn(sessions))
        assert sessions.calls == ["get_or_create", "reset", "release"]

    def test_a_second_server_beside_the_injection_still_refuses(self, monkeypatch, registered):
        path = _install(monkeypatch)

        def inject_both() -> None:
            _inject(path)
            spec = json.loads(path.read_text())
            spec["mcpServers"]["planted"] = {"command": "planted"}
            path.write_text(json.dumps(spec))

        sessions = _sessions(ACP_BACKEND_KAS, on_spawn=inject_both)

        with pytest.raises(RuntimeError, match="not acquired"):
            asyncio.run(_turn(sessions))
        assert sessions.calls == ["get_or_create", "reset", "release"]

    def test_with_no_filter_registered_the_injection_still_refuses(self, monkeypatch):
        path = _install(monkeypatch)
        sessions = _sessions(ACP_BACKEND_KAS, on_spawn=lambda: _inject(path))

        with pytest.raises(RuntimeError, match="not acquired"):
            asyncio.run(_turn(sessions))
        assert sessions.calls == ["get_or_create", "reset", "release"]


class TestABackgroundTurnOnKiroCli:
    def test_a_registered_injection_still_refuses(self, monkeypatch, registered):
        """kiro-cli loads the file on disk, so the injection would run there."""
        path = _install(monkeypatch)
        sessions = _sessions("", on_spawn=lambda: _inject(path))

        with pytest.raises(RuntimeError, match="not acquired"):
            asyncio.run(_turn(sessions))
        assert sessions.calls == ["get_or_create", "reset", "release"]

    def test_a_read_taken_for_kas_does_not_vouch_for_a_kiro_cli_start(
        self, monkeypatch, registered
    ):
        path = _install(monkeypatch)
        clean = path.read_text()
        _inject(path)
        sessions = _Sessions(
            _provider(""), ACP_BACKEND_KAS, on_spawn=lambda: path.write_text(clean)
        )

        with pytest.raises(RuntimeError, match="not acquired"):
            asyncio.run(_turn(sessions))
        assert sessions.calls == ["get_or_create", "reset", "release"]


class TestThePostStartCheckOnKas:
    @pytest.mark.parametrize("before", [True, False])
    def test_it_needs_the_pre_spawn_read_too(self, monkeypatch, registered, before):
        """The spec KAS was sent is judged after the start, and the read before the
        spawn must also have vouched for it."""
        _inject(_install(monkeypatch))
        client = _provider(ACP_BACKEND_KAS).client

        assert client.effective_spec_declares_zero_tools(authored_before_spawn=before) is before


class TestThePreSpawnVerdict:
    def test_on_kas_a_registered_injection_reads_as_the_template(self, monkeypatch, registered):
        from kiro_crew.agent_sdk.agent_specs import authored_spec_toolless_verdict

        _inject(_install(monkeypatch))

        assert authored_spec_toolless_verdict(_LITE, backend=ACP_BACKEND_KAS) is True
        assert authored_spec_toolless_verdict(_LITE, backend="") is False
        assert authored_spec_toolless_verdict(_LITE) is False


class TestWhatKasIsSent:
    def test_the_template_instead_of_the_injected_file(self, monkeypatch, registered):
        from kiro_crew.acp.harness.kas import resolve_projected_spec
        from kiro_crew.agent import kiro_agents_dir_path

        _inject(_install(monkeypatch))

        spec, _dir = resolve_projected_spec(kiro_agents_dir_path(), _LITE, None)

        assert spec.get("mcpServers") == {}
        assert "allowedTools" not in spec

    def test_the_file_when_no_filter_matches(self, monkeypatch, registered):
        from kiro_crew.acp.harness.kas import resolve_projected_spec
        from kiro_crew.agent import kiro_agents_dir_path

        _inject(_install(monkeypatch), entry={**_ENTRY, "args": ["other"]})

        spec, _dir = resolve_projected_spec(kiro_agents_dir_path(), _LITE, None)

        assert _SERVER in spec.get("mcpServers", {})

    def test_every_broker_stub_it_does_not_mount_is_withdrawn(
        self, monkeypatch, tmp_path, registered
    ):
        from kiro_crew.acp.harness.kas import KasHarness

        _inject(_install(monkeypatch))
        extras = asyncio.run(
            KasHarness().session_extras(
                _LITE,
                work_dir=str(tmp_path),
                mcp_gateway_overlay=object(),
                worker_stub_names=frozenset({_SERVER}),
            )
        )

        assert extras.withdrawn_stub_names == frozenset({_SERVER})

    def test_the_runtime_captures_its_stub_names_on_kas_only(self):
        from kiro_crew.acp.runtime_session_setup import _kas_worker_stub_names

        pooled = [{"name": _SERVER}]

        assert _kas_worker_stub_names(ACP_BACKEND_KAS, _LITE, "", pooled) == frozenset({_SERVER})
        assert _kas_worker_stub_names("", _LITE, "", pooled) is None
        assert _kas_worker_stub_names(ACP_BACKEND_KAS, "kirocrew", "k", pooled) is None
