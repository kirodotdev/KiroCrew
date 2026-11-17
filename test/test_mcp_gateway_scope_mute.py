"""A server muted in a config scope is never wrapped into a gateway stub.

``POST /api/mcp/toggle`` writes the mute into the kiro global or the crew store,
never into an agent file it does not own. A wrapped name is injected as a
session-level stub and subtracted from the KAS projection before that
projection's own scope check runs, so the wrap decision has to read the scopes
itself. These drive the real
``rewrite_agents`` over real files and read the session's servers back through the
same lookups the KAS harness uses.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from kiro_crew.acp.harness.kas import withdrawn_worker_stubs
from kiro_crew.acp.kas_agents import build_kas_custom_agents
from kiro_crew.mcp_gateway.rewriter import rewrite_agents
from kiro_crew.mcp_gateway.session_servers import injection_server_names, pooled_session_servers

AGENT = "probe-agent"
SERVER = "oauth-server"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A kiro home (agents + settings) and a crew store, both pinned as the scopes."""
    from kiro_crew import mcp_discovery

    agents = tmp_path / "kiro" / "agents"
    agents.mkdir(parents=True)
    kiro_global = tmp_path / "kiro" / "settings" / "mcp.json"
    kiro_global.parent.mkdir(parents=True)
    store = tmp_path / "crew" / "mcp.json"
    store.parent.mkdir(parents=True)
    monkeypatch.setattr(
        mcp_discovery, "_MCP_SOURCES", ((store, "kirocrew"), (kiro_global, "kiroGlobal"))
    )
    monkeypatch.setattr(mcp_discovery, "_MCP_JSON_PATHS", None)
    return tmp_path, agents, kiro_global, store


def _write(path: Path, servers: dict) -> None:
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def _agent(agents: Path, servers: dict) -> None:
    (agents / f"{AGENT}.json").write_text(
        json.dumps({"name": AGENT, "mcpServers": servers, "tools": ["*"]}), encoding="utf-8"
    )


def _rewrite(root: Path, agents: Path) -> Path:
    overlay = root / "overlay" / "agents"
    rewrite_agents(
        source_dir=agents,
        overlay_dir=overlay,
        socket_path=root / "gw.sock",
        work_dir=root / "wd",
        stub_servers=frozenset({SERVER}),
    )
    return overlay


def _session_servers(overlay: Path) -> set[str]:
    return {e["name"] for e in pooled_session_servers(overlay, AGENT)}


def _kas_declared(root: Path, overlay: Path) -> set[str]:
    spec = json.loads((root / "kiro" / "agents" / f"{AGENT}.json").read_text(encoding="utf-8"))
    built = build_kas_custom_agents(
        root,
        AGENT,
        spec,
        stub_server_names=injection_server_names(overlay, AGENT),
    )
    return set(built[0].get("mcpServers", {}))


LIVE = {"command": sys.executable, "args": ["-c", "pass"]}


class TestAnAgentDeclaredServer:
    @pytest.mark.parametrize("scope", ["store", "global"])
    def test_a_scope_mute_starts_it_nowhere(self, home, scope):
        root, agents, kiro_global, store = home
        _agent(agents, {SERVER: dict(LIVE)})
        # The toggle's own shape for a server no scope file held: a bare stub.
        _write(store if scope == "store" else kiro_global, {SERVER: {"disabled": True}})
        overlay = _rewrite(root, agents)
        assert SERVER not in _session_servers(overlay)
        assert SERVER not in _kas_declared(root, overlay)

    def test_an_unmuted_one_is_still_stubbed(self, home):
        root, agents, _kiro_global, store = home
        _agent(agents, {SERVER: dict(LIVE)})
        _write(store, {SERVER: {"command": "x"}})
        overlay = _rewrite(root, agents)
        assert _session_servers(overlay) == {SERVER}
        # Stubbed, so the projection leaves it to the session-level entry.
        assert SERVER not in _kas_declared(root, overlay)

    def test_a_mute_after_a_cached_pass_still_lands(self, home):
        """The overlay fingerprint must see the scope mute, or a gateway restart
        serves the stub the earlier pass wrote."""
        root, agents, _kiro_global, store = home
        _agent(agents, {SERVER: dict(LIVE)})
        overlay = _rewrite(root, agents)
        assert _session_servers(overlay) == {SERVER}
        _write(store, {SERVER: {"disabled": True}})
        overlay = _rewrite(root, agents)
        assert SERVER not in _session_servers(overlay)

    def test_a_mute_under_the_raw_key_withholds_the_alias(self, home):
        root, agents, _kiro_global, store = home
        alias = "playwright-mcp"
        _agent(agents, {alias: dict(LIVE)})
        _write(store, {"npm:@playwright/mcp": {"disabled": True}})
        overlay = root / "overlay" / "agents"
        rewrite_agents(
            source_dir=agents,
            overlay_dir=overlay,
            socket_path=root / "gw.sock",
            work_dir=root / "wd",
            stub_servers=frozenset({alias}),
        )
        assert alias not in {e["name"] for e in pooled_session_servers(overlay, AGENT)}


class TestAGlobalSettingsServer:
    def test_a_store_mute_keeps_it_out_of_every_overlay(self, home):
        root, agents, kiro_global, store = home
        _agent(agents, {})
        _write(kiro_global, {SERVER: dict(LIVE)})
        _write(store, {SERVER: {"disabled": True}})
        overlay = _rewrite(root, agents)
        assert SERVER not in _session_servers(overlay)

    def test_an_unmuted_one_is_still_injected(self, home):
        root, agents, kiro_global, _store = home
        _agent(agents, {})
        _write(kiro_global, {SERVER: dict(LIVE)})
        overlay = _rewrite(root, agents)
        assert _session_servers(overlay) == {SERVER}


class TestAWorkerStub:
    """``withdrawn_worker_stubs``: a crewmate worker's stubs were picked before its
    spec was read, so a mute in between withdraws the stub too."""

    def test_a_scope_mute_withdraws_it(self, home):
        _root, _agents, _kiro_global, store = home
        _write(store, {SERVER: {"disabled": True}})
        spec = {"mcpServers": {SERVER: dict(LIVE)}}
        assert withdrawn_worker_stubs(spec, frozenset({SERVER})) == {SERVER}

    def test_an_unmuted_one_is_kept(self, home):
        spec = {"mcpServers": {SERVER: dict(LIVE)}}
        assert withdrawn_worker_stubs(spec, frozenset({SERVER})) == frozenset()

    def test_the_entrys_own_mute_still_withdraws_it(self, home):
        spec = {"mcpServers": {SERVER: {**LIVE, "disabled": True}}}
        assert withdrawn_worker_stubs(spec, frozenset({SERVER})) == {SERVER}


class TestAKeepWhenTheSettingsFileIsUnsignable:
    """The settings file is one of the scopes, so a fault on it shrinks this pass's
    reading of the mutes. A keep holds the reading only to "no new mute"."""

    @staticmethod
    def _keep(prev: list[str], cur: list[str]) -> bool:
        from kiro_crew.mcp_gateway.rewriter_overlay import _overlay_inputs_unchanged

        sources = {"a.json": [1, 2, "d"]}
        stored = {"inputs": {"sources": sources, "settings": [1], "scope_muted": prev}}
        current = {"sources": sources, "settings": None, "scope_muted": cur}
        return _overlay_inputs_unchanged(stored, current, source_name="a.json")

    def test_a_reading_that_shrank_keeps(self):
        assert self._keep(["a", "b"], ["a"]) is True

    def test_a_new_mute_refuses_the_keep(self):
        assert self._keep(["a"], ["a", "b"]) is False

    def test_a_signable_pass_compares_it_exactly(self):
        from kiro_crew.mcp_gateway.rewriter_overlay import _overlay_inputs_unchanged

        sources = {"a.json": [1]}
        stored = {"inputs": {"sources": sources, "settings": [1], "scope_muted": ["a"]}}
        current = {"sources": sources, "settings": [1], "scope_muted": []}
        assert _overlay_inputs_unchanged(stored, current, source_name="a.json") is False


class TestACrewmateOverlay:
    """The crewmate half of the pass feeds a worker session's stubs, so a scope
    mute keeps the stub out of it too, and an unmuted server keeps its stub."""

    @pytest.fixture
    def mates(self, home, monkeypatch):
        from kiro_crew import mate_config

        root, agents, _kiro_global, store = home
        _agent(agents, {})
        mates = root / "mates" / "agents"
        mates.mkdir(parents=True)
        (mates / "pixel.json").write_text(
            json.dumps({"name": "pixel", "tools": ["*"], "mcpServers": {SERVER: dict(LIVE)}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(mate_config, "crewmate_file_on_record", lambda mate_id: True)
        return root, agents, mates, store

    @staticmethod
    def _stubbed(root: Path, agents: Path, mates: Path) -> bool:
        from kiro_crew.mcp_gateway import mate_overlays
        from kiro_crew.mcp_gateway.rewriter import _WRAPPER_MARKER

        overlay = root / "overlay" / "agents"
        rewrite_agents(
            source_dir=agents,
            overlay_dir=overlay,
            socket_path=root / "gw.sock",
            work_dir=root / "wd",
            stub_servers=frozenset({SERVER}),
            mate_source_dir=mates,
        )
        spec = json.loads((mate_overlays.mate_overlay_dir(overlay) / "pixel.json").read_text())
        return spec["mcpServers"][SERVER].get(_WRAPPER_MARKER) is True

    def test_a_scope_mute_leaves_it_unwrapped(self, mates):
        root, agents, mate_dir, store = mates
        _write(store, {SERVER: {"disabled": True}})
        assert self._stubbed(root, agents, mate_dir) is False

    def test_an_unmuted_one_is_still_wrapped(self, mates):
        root, agents, mate_dir, _store = mates
        assert self._stubbed(root, agents, mate_dir) is True
