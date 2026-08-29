"""Every remaining agent-spec scan reads through the hardened reader (#6695).

Seven call sites read ``~/.kiro/agents/*.json`` with a hand-rolled
``json.loads(read_text())`` until this migration; each now goes through
``agent_discovery._read_agent_spec`` -- the size-capped, sensitive-symlink- and
non-object-refusing reader #5423 adopted for ``_resolve_agent_model``. Per
surface this pins the two properties the migration promises: a refused spec is
SKIPPED (it degrades exactly like an absent one, and the surface still
answers), and a valid spec is unaffected under the same cap.

Refusal is exercised with a LOWERED ``hooks.MAX_FILE_BYTES`` (the property is
that the cap is consulted, not its value) and with non-object JSON -- both
observable without planting symlinks, mirroring #5423's tests. One
representative symlink test proves the sensitive-target guard applies through
a migrated caller; the guard itself lives in ``_read_agent_spec`` and has its
own coverage.

#6736 extends the migration to three more raw ``_load_json`` reads of
``kirocrew.json`` (``mint._write_mint_agent_spec``,
``mint._agent_spec_entry_missing``, ``agent._install_heartbeat_agent``); their
classes below pin the same two properties per site. For those three sites only
the ``oversized`` and symlink cases are differential against the old path
(``_load_json`` already normalized a non-object root to ``{}``); the
``non_object`` cases are kept as non-differential regression pins.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web

from kiro_crew.agent import _install_heartbeat_agent, migrate_agent_specs
from kiro_crew.agent_files import AGENT_FILENAME, HEARTBEAT_AGENT_FILENAME
from kiro_crew.connections import mint
from kiro_crew.dashboard.chat_persistence import _build_kiro_model_map
from kiro_crew.dashboard.handlers.agents import (
    _namespaced_agent_file_exists,
    api_agent_detail,
)
from kiro_crew.dashboard.handlers.mcp import (
    _collect_server_rows,
    _launch_specs_for,
    api_mcp_active,
)

# The two refusal shapes cheap enough to plant per surface. "oversized" is the
# differential case (the old read_text path had no cap, so it PARSED these);
# "non_object" pins that valid-JSON-wrong-shape degrades as absent everywhere,
# including the surfaces whose old parse crashed on it (AttributeError past an
# ``except (JSONDecodeError, OSError)``).
REFUSALS = ("oversized", "non_object")


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    """Isolated agents dir behind a lowered read cap.

    ``KIRO_AGENTS_DIR`` is the documented override hook every migrated site
    resolves through ``kiro_agents_dir_path()``; the cap is lowered rather than
    writing a real 50 MB fixture (same trade #5423's tests made).
    """
    from kiro_crew import hooks

    monkeypatch.setattr(hooks, "MAX_FILE_BYTES", 256)
    monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", tmp_path)
    return tmp_path


def _plant(agents_dir: Path, filename: str, spec: dict) -> Path:
    p = agents_dir / filename
    p.write_text(json.dumps(spec), encoding="utf-8")
    return p


def _plant_refused(agents_dir: Path, filename: str, spec: dict, kind: str) -> Path:
    p = agents_dir / filename
    if kind == "oversized":
        body = dict(spec)
        body["pad"] = "x" * 1024  # far past the lowered 256-byte cap
        p.write_text(json.dumps(body), encoding="utf-8")
    else:  # non_object: valid JSON, wrong shape
        p.write_text(json.dumps([spec]), encoding="utf-8")
    return p


class TestMigrateAgentSpecs:
    """agent.migrate_agent_specs -- the one site that also WRITES."""

    @pytest.mark.parametrize("kind", REFUSALS)
    def test_refused_spec_is_never_rewritten(self, agents_dir, kind):
        """A spec the reader refuses is not cleaned AND not written back.

        Strictly safer than the old path, which read (and rewrote) whatever
        the file held: refusal now keeps the write from happening at all.
        """
        p = _plant_refused(agents_dir, "dirty.json", {"name": "dirty", "model_managed": True}, kind)
        before = p.read_text(encoding="utf-8")

        assert migrate_agent_specs() == 0
        assert p.read_text(encoding="utf-8") == before

    def test_valid_spec_still_cleaned_under_the_same_cap(self, agents_dir):
        p = _plant(agents_dir, "dirty.json", {"name": "dirty", "model_managed": True})

        assert migrate_agent_specs() == 1
        assert "model_managed" not in json.loads(p.read_text(encoding="utf-8"))


class TestBuildKiroModelMap:
    """chat_persistence._build_kiro_model_map -- feeds legacy session restore."""

    @pytest.mark.parametrize("kind", REFUSALS)
    def test_refused_spec_is_skipped_not_fatal(self, agents_dir, kind):
        """The refused file contributes nothing and the scan keeps going.

        Under the old parse a non-object spec raised past the inner except and
        aborted the whole scan through the outer one; now it is a per-file skip.
        """
        _plant_refused(agents_dir, "bad.json", {"name": "bad", "model": "pinned-by-bad"}, kind)
        _plant(agents_dir, "good.json", {"name": "good", "model": "pinned-by-good"})

        out = _build_kiro_model_map()

        assert out.get("good") == "pinned-by-good"
        assert "bad" not in out

    def test_valid_spec_still_maps_under_the_same_cap(self, agents_dir):
        _plant(agents_dir, "good.json", {"name": "good", "model": "pinned-by-good"})

        out = _build_kiro_model_map()

        # Keyed by both the declared name and the file stem (here identical).
        assert out == {"good": "pinned-by-good"}


class TestNamespacedAgentFileExists:
    """handlers.agents._namespaced_agent_file_exists -- the app-agent probe."""

    @pytest.mark.parametrize("kind", REFUSALS)
    def test_refused_spec_does_not_back_the_agent(self, agents_dir, kind):
        _plant_refused(agents_dir, "app--probe.json", {"name": "probe"}, kind)

        assert _namespaced_agent_file_exists("probe") is False

    def test_valid_spec_still_backs_the_agent(self, agents_dir):
        _plant(agents_dir, "app--probe.json", {"name": "probe"})

        assert _namespaced_agent_file_exists("probe") is True


def _detail_request(name: str) -> MagicMock:
    request = MagicMock(spec=web.Request)
    request.method = "GET"
    request.match_info = {"name": name}
    request.app = {"state": MagicMock()}
    return request


class TestApiAgentDetail:
    """handlers.agents.api_agent_detail -- GET by-name lookup."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", REFUSALS)
    async def test_refused_spec_reads_as_absent(self, agents_dir, kind):
        """A refused spec is a 404, not a 500: the old parse let a non-object
        file escape as AttributeError past ``except (JSONDecodeError, OSError)``."""
        _plant_refused(agents_dir, "ghost.json", {"name": "ghost"}, kind)

        resp = await api_agent_detail(_detail_request("ghost"))

        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_valid_spec_still_served_under_the_same_cap(self, agents_dir):
        _plant(agents_dir, "real.json", {"name": "real", "model": "pinned-by-real"})

        resp = await api_agent_detail(_detail_request("real"))

        assert resp.status == 200
        assert json.loads(resp.text)["name"] == "real"


def _mcp_request(agent: str) -> MagicMock:
    request = MagicMock(spec=web.Request)
    request.query = {"agent": agent}
    return request


@pytest.fixture
def identity_bindings(monkeypatch):
    """Bind every Kiro Crew agent name to a same-named kiro agent.

    Without this the real resolver maps an unknown name onto the ``kirocrew``
    default, so ``/api/mcp/active`` would always take the global-scope branch
    and the per-agent branch under test would be unreachable (same fixture
    shape as test_handlers_mcp_coverage.py).
    """
    from types import SimpleNamespace

    import kiro_crew.config.loader as loader

    monkeypatch.setattr(
        loader,
        "resolve_agent_bindings",
        lambda cfg, name: SimpleNamespace(kiro_agent=name),
    )


class TestApiMcpActive:
    """handlers.mcp.api_mcp_active -- per-agent mcpServers list."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", REFUSALS)
    async def test_refused_spec_reads_as_absent(self, agents_dir, identity_bindings, kind):
        _plant_refused(
            agents_dir, "probe.json", {"name": "probe-6695", "mcpServers": {"srv": {}}}, kind
        )

        resp = await api_mcp_active(_mcp_request("probe-6695"))

        assert resp.status == 200
        assert json.loads(resp.text) == []

    @pytest.mark.asyncio
    async def test_valid_spec_still_lists_servers(self, agents_dir, identity_bindings):
        _plant(agents_dir, "probe.json", {"name": "probe-6695", "mcpServers": {"b": {}, "a": {}}})

        resp = await api_mcp_active(_mcp_request("probe-6695"))

        assert json.loads(resp.text) == [
            {"name": "a", "enabled": True},
            {"name": "b", "enabled": True},
        ]


class TestCollectServerRows:
    """handlers.mcp._collect_server_rows -- the fleet row scan."""

    @pytest.mark.parametrize("kind", REFUSALS)
    def test_refused_spec_contributes_no_rows(self, agents_dir, kind):
        _plant_refused(
            agents_dir,
            "bad.json",
            {"name": "bad", "mcpServers": {"phantom": {"command": "x"}}},
            kind,
        )

        assert "phantom" not in _collect_server_rows()

    def test_valid_spec_rows_survive_the_same_cap(self, agents_dir):
        _plant(agents_dir, "good.json", {"name": "good", "mcpServers": {"real": {"command": "x"}}})

        assert "real" in _collect_server_rows()


class TestLaunchSpecsFor:
    """handlers.mcp._launch_specs_for -- the batch-stub spec collection."""

    @pytest.mark.parametrize("kind", REFUSALS)
    def test_refused_spec_contributes_no_launch_specs(self, agents_dir, kind):
        _plant_refused(
            agents_dir,
            "bad.json",
            {"name": "bad", "mcpServers": {"srv": {"command": "x"}}},
            kind,
        )

        assert _launch_specs_for({"srv"}) == {}

    def test_valid_spec_still_yields_a_launch_spec(self, agents_dir):
        _plant(agents_dir, "good.json", {"name": "good", "mcpServers": {"srv": {"command": "x"}}})

        specs = _launch_specs_for({"srv"})

        assert "srv" in specs
        assert specs["srv"][0].command == "x"


class TestSensitiveSymlinkGuard:
    """One representative surface proves the symlink guard flows through.

    The guard's own matrix lives with ``_read_agent_spec``; this pins that a
    migrated caller actually consults it (same shape as #5423's test).
    """

    def test_link_to_a_sensitive_target_is_refused(self, tmp_path, monkeypatch):
        from kiro_crew import agent_discovery

        target = tmp_path / "protected.json"
        target.write_text(json.dumps({"name": "linked", "model": "leaked-value"}))
        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "linked.json").symlink_to(target)
        monkeypatch.setattr(agent_discovery, "is_sensitive_path", lambda p: str(target) in str(p))
        monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", agents)

        out = _build_kiro_model_map()

        assert "linked" not in out


class TestWriteMintAgentSpec:
    """connections.mint._write_mint_agent_spec -- the one-server mint spec (#6736).

    A refused main spec must FAIL the mint (raise): the main-agent fallback
    spawns ``kiro-cli --agent kirocrew``, and the child would reload the very
    file the gateway just refused. The fallback stays reserved for a genuinely
    absent file or alias entry. Under the old ``_load_json`` path an oversized
    main spec was parsed and minted from.
    """

    @pytest.mark.parametrize("kind", REFUSALS)
    def test_refused_main_spec_fails_the_mint(self, agents_dir, monkeypatch, kind):
        # Stubbed for hermeticity: the refusal path never records a mint spec,
        # but a regression that DID mint must not write a real manifest.
        monkeypatch.setattr(mint, "_record_mint_spec", lambda spec_path: True)
        alias = mint.mcp_server_alias("probe")
        _plant_refused(agents_dir, AGENT_FILENAME, {"mcpServers": {alias: {"command": "x"}}}, kind)

        with pytest.raises(OSError, match="main agent spec unusable"):
            mint._write_mint_agent_spec("probe")

    def test_absent_main_spec_still_falls_back_to_the_main_agent(self, agents_dir):
        assert mint._write_mint_agent_spec("probe") == (mint._MAIN_AGENT_NAME, "")

    def test_valid_main_spec_still_mints_under_the_same_cap(self, agents_dir, monkeypatch):
        monkeypatch.setattr(mint, "_record_mint_spec", lambda spec_path: True)
        alias = mint.mcp_server_alias("probe")
        _plant(agents_dir, AGENT_FILENAME, {"mcpServers": {alias: {"command": "x"}}})

        name, path = mint._write_mint_agent_spec("probe")

        # Names carry a per-mint random suffix; pin the stable properties:
        # a real (non-fallback) mint whose file matches the returned name.
        assert name != mint._MAIN_AGENT_NAME
        assert path == str(agents_dir / f"{name}.json")
        written = json.loads(Path(path).read_text(encoding="utf-8"))
        assert written["mcpServers"] == {alias: {"command": "x"}}


class TestAgentSpecEntryMissing:
    """connections.mint._agent_spec_entry_missing -- the concurrent-uninstall probe (#6736).

    A refused main spec reads as absent, so the entry counts as missing; the old
    path PARSED an oversized spec and reported the entry present.
    """

    @pytest.mark.parametrize("kind", REFUSALS)
    def test_refused_main_spec_reads_as_entry_missing(self, agents_dir, kind):
        alias = mint.mcp_server_alias("probe")
        _plant_refused(agents_dir, AGENT_FILENAME, {"mcpServers": {alias: {"command": "x"}}}, kind)

        assert mint._agent_spec_entry_missing("probe") is True

    def test_valid_main_spec_entry_still_found_under_the_same_cap(self, agents_dir):
        alias = mint.mcp_server_alias("probe")
        _plant(agents_dir, AGENT_FILENAME, {"mcpServers": {alias: {"command": "x"}}})

        assert mint._agent_spec_entry_missing("probe") is False

    def test_link_to_a_sensitive_target_reads_as_entry_missing(self, tmp_path, monkeypatch):
        """The sensitive-symlink guard flows through a migrated mint caller."""
        from kiro_crew import agent_discovery

        alias = mint.mcp_server_alias("probe")
        target = tmp_path / "protected.json"
        target.write_text(json.dumps({"mcpServers": {alias: {"command": "x"}}}), encoding="utf-8")
        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / AGENT_FILENAME).symlink_to(target)
        monkeypatch.setattr(agent_discovery, "is_sensitive_path", lambda p: str(target) in str(p))
        monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", agents)

        assert mint._agent_spec_entry_missing("probe") is True


class TestInstallHeartbeatAgent:
    """agent._install_heartbeat_agent -- the main-config mcpServers pull (#6736).

    A refused main spec contributes no MCP entries (the heartbeat agent installs
    with an empty toolset, same as when the main entry does not exist yet); the
    old path parsed an oversized main spec and copied its entry through.
    """

    @pytest.fixture
    def heartbeat_env(self, monkeypatch):
        """Keep the install local: no config load, no cc-model sidecar write."""
        monkeypatch.setattr("kiro_crew.agent._background_agent_model", lambda: "auto")
        monkeypatch.setattr("kiro_crew.agent.agent_state.set_cc_model", lambda *a, **k: None)

    @pytest.mark.parametrize("kind", REFUSALS)
    def test_refused_main_spec_yields_no_mcp_servers(self, agents_dir, heartbeat_env, kind):
        _plant_refused(
            agents_dir,
            AGENT_FILENAME,
            {"mcpServers": {"kirocrew-core": {"command": "x"}}},
            kind,
        )

        _install_heartbeat_agent()

        written = json.loads((agents_dir / HEARTBEAT_AGENT_FILENAME).read_text(encoding="utf-8"))
        assert written["mcpServers"] == {}
        assert written["tools"] == []

    def test_valid_main_spec_still_feeds_the_heartbeat_agent(self, agents_dir, heartbeat_env):
        _plant(
            agents_dir,
            AGENT_FILENAME,
            {"mcpServers": {"kirocrew-core": {"command": "x", "args": ["--include-tools", "a"]}}},
        )

        _install_heartbeat_agent()

        written = json.loads((agents_dir / HEARTBEAT_AGENT_FILENAME).read_text(encoding="utf-8"))
        assert written["mcpServers"]["kirocrew-core"]["args"] == []
        assert written["tools"] == ["@kirocrew-core"]
