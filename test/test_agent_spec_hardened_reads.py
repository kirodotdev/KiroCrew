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

import ast
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web

from conftest import requires_symlinks
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
    _find_server_spec_anywhere,
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


@pytest.fixture
def agents_dir_resolver(agents_dir, monkeypatch):
    """``agents_dir`` plus the ``config.paths`` resolver these two sites use.

    They call ``config.paths.kiro_agents_dir()``, which honours its OWN override
    and NOT ``agent.KIRO_AGENTS_DIR``, so the base fixture alone does not
    redirect them. ``cron_script._resolve_mcp_server`` is ``lru_cache``d, so the
    cache is cleared around the test instead of leaking a resolved answer into
    the next one.
    """
    from kiro_crew.config import paths as config_paths
    from kiro_crew.cron_script import _resolve_mcp_server

    monkeypatch.setattr(config_paths, "_agents_dir_override", lambda: agents_dir)
    _resolve_mcp_server.cache_clear()
    yield agents_dir
    _resolve_mcp_server.cache_clear()


class TestFindServerSpecAnywhere:
    """handlers.mcp._find_server_spec_anywhere -- the spec-recovery search.

    Server names carry a suffix because the search falls through to the provider
    ``mcp.json`` scopes, which are real files on a developer machine; a generic
    name could be answered by one of those and read as a pass.
    """

    @pytest.mark.parametrize("kind", REFUSALS)
    def test_refused_agent_spec_contributes_no_spec(self, agents_dir_resolver, kind):
        _plant_refused(
            agents_dir_resolver,
            AGENT_FILENAME,
            {"name": "kirocrew", "mcpServers": {"phantom-hr": {"command": "x"}}},
            kind,
        )

        assert _find_server_spec_anywhere("phantom-hr") is None

    def test_valid_agent_spec_still_found_under_the_same_cap(self, agents_dir_resolver):
        _plant(
            agents_dir_resolver,
            AGENT_FILENAME,
            {"name": "kirocrew", "mcpServers": {"real-hr": {"command": "x", "disabled": True}}},
        )

        assert _find_server_spec_anywhere("real-hr") == {"command": "x"}


class TestResolveMcpServer:
    """cron_script._resolve_mcp_server -- the zero-token script-cron path."""

    @pytest.mark.parametrize("kind", REFUSALS)
    def test_refused_agent_spec_resolves_to_no_server(self, agents_dir_resolver, kind):
        from kiro_crew.cron_script import _resolve_mcp_server

        _plant_refused(
            agents_dir_resolver,
            AGENT_FILENAME,
            {"name": "kirocrew", "mcpServers": {"srv-hr": {"command": "c", "args": ["a"]}}},
            kind,
        )

        assert _resolve_mcp_server("srv-hr") is None

    def test_valid_agent_spec_still_resolves_under_the_same_cap(self, agents_dir_resolver):
        from kiro_crew.cron_script import _resolve_mcp_server

        _plant(
            agents_dir_resolver,
            AGENT_FILENAME,
            {"name": "kirocrew", "mcpServers": {"srv-hr": {"command": "c", "args": ["a"]}}},
        )

        # #2602 widened the resolver contract to ``(argv, env)`` -- a spec with
        # no env block resolves to an empty dict, not None.
        assert _resolve_mcp_server("srv-hr") == (("c", "a"), {})

    def test_a_declared_env_block_survives_the_hardened_read(self, agents_dir_resolver):
        """The other half of the post-#2602 contract, on THIS module's read path.

        The case above pins the empty-env shape, which a resolver that dropped the
        block entirely would also satisfy. #2602 added ``env`` because a launcher
        that shells out to a helper reachable only via the PATH the config supplies
        dies at the JSON-RPC ``initialize`` handshake without it -- so the value has
        to arrive, not just the tuple shape. Nothing here asserted that: this file's
        other cases are refusals asserting ``is None``, and the accepted branch is
        the size-capped hardened read, which is exactly where a future tightening
        could drop the block with the argv assertion still green.
        """
        from kiro_crew.cron_script import _resolve_mcp_server

        _plant(
            agents_dir_resolver,
            AGENT_FILENAME,
            {
                "name": "kirocrew",
                "mcpServers": {
                    "srv-hr": {
                        "command": "c",
                        "args": ["a"],
                        "env": {"PATH": "/opt/tools/bin"},
                    }
                },
            },
        )

        assert _resolve_mcp_server("srv-hr") == (("c", "a"), {"PATH": "/opt/tools/bin"})

    def test_malformed_spec_no_longer_escapes_as_a_decode_error(self, agents_dir_resolver):
        """The differential case for THIS site.

        The old bare ``read_text`` + ``json.loads`` had no ``except`` anywhere on
        the path, so a half-written spec raised ``JSONDecodeError`` out of the
        resolver and into the cron runner. Refusal now degrades to "no such
        server", the same answer an absent entry gives.
        """
        from kiro_crew.cron_script import _resolve_mcp_server

        (agents_dir_resolver / AGENT_FILENAME).write_text("{not json", encoding="utf-8")

        assert _resolve_mcp_server("srv-hr") is None


class TestLoadAgentConfig:
    """mcp_discovery._load_agent_config -- the merged mcpServers view.

    Feeds MCP discovery from several surfaces, which is why its label is
    ``unknown`` rather than one interface channel.
    """

    @pytest.mark.parametrize("kind", REFUSALS)
    def test_refused_agent_spec_contributes_no_servers(self, agents_dir_resolver, kind):
        from kiro_crew.mcp_discovery import _load_agent_config

        _plant_refused(
            agents_dir_resolver,
            AGENT_FILENAME,
            {"name": "kirocrew", "mcpServers": {"phantom-md": {"command": "x"}}},
            kind,
        )

        assert "phantom-md" not in _load_agent_config().get("mcpServers", {})

    def test_valid_agent_spec_still_merges_under_the_same_cap(self, agents_dir_resolver):
        from kiro_crew.mcp_discovery import _load_agent_config

        _plant(
            agents_dir_resolver,
            AGENT_FILENAME,
            {"name": "kirocrew", "mcpServers": {"real-md": {"command": "x"}}},
        )

        assert "real-md" in _load_agent_config().get("mcpServers", {})

    def test_non_utf8_spec_no_longer_escapes_the_handler(self, agents_dir_resolver):
        """The differential case for THIS site.

        The old form passed ``encoding="utf-8"`` and caught only
        ``(JSONDecodeError, OSError)``, so a non-UTF-8 spec raised
        ``UnicodeDecodeError`` -- a ``ValueError`` -- straight through.
        """
        from kiro_crew.mcp_discovery import _load_agent_config

        (agents_dir_resolver / AGENT_FILENAME).write_bytes(b'{"mcpServers": {"\xff": {}}}')

        assert "phantom-md" not in _load_agent_config().get("mcpServers", {})


class TestLoadSteeringResources:
    """context._load_steering_resources -- dashboard steering injection.

    ``unknown`` labels it because dashboard chat, Slack and cron sessions all
    reach it through the same context builder.

    The loader only accepts a ``file://`` resource that globs under
    ``Path.home()`` and resolves inside it, so ``Path.home`` itself is redirected
    at the isolated agents dir and the pattern is written relative to it.
    Redirecting the resolver rather than ``HOME`` is what makes this work on
    every platform: Windows ``expanduser`` reads ``USERPROFILE``, so setting
    ``HOME`` alone left the real profile dir in play, the marker could never
    load, and the refusal assertion passed vacuously while the valid-spec one
    failed (same ``setattr(Path, "home", ...)`` shape as test_acp_client.py).
    """

    @staticmethod
    def _plant_steering(agents_dir: Path, monkeypatch) -> None:
        monkeypatch.setattr(Path, "home", staticmethod(lambda: agents_dir))
        (agents_dir / "steer.md").write_text("STEERING-MARKER", encoding="utf-8")

    @pytest.mark.parametrize("kind", REFUSALS)
    def test_refused_agent_spec_loads_no_steering(self, agents_dir_resolver, monkeypatch, kind):
        from kiro_crew.context import _load_steering_resources

        self._plant_steering(agents_dir_resolver, monkeypatch)
        _plant_refused(
            agents_dir_resolver,
            AGENT_FILENAME,
            {"name": "kirocrew", "resources": ["file://steer.md"]},
            kind,
        )

        assert "STEERING-MARKER" not in _load_steering_resources()

    def test_valid_agent_spec_still_loads_steering_under_the_same_cap(
        self, agents_dir_resolver, monkeypatch
    ):
        from kiro_crew.context import _load_steering_resources

        self._plant_steering(agents_dir_resolver, monkeypatch)
        _plant(
            agents_dir_resolver,
            AGENT_FILENAME,
            {"name": "kirocrew", "resources": ["file://steer.md"]},
        )

        assert "STEERING-MARKER" in _load_steering_resources()

    def test_absent_spec_still_returns_empty(self, agents_dir_resolver):
        from kiro_crew.context import _load_steering_resources

        assert _load_steering_resources() == ""


class TestDenialAuditNeverRaises:
    """A failing denial audit must not break either never-raise promise.

    The refusal paths are the only places this module calls out to another
    subsystem, and for some surfaces it is the process's FIRST SEL use --
    constructing that singleton mkdirs its home. On an unwritable or hostile SEL
    directory the audit therefore raises, and BOTH callers promise not to:
    ``_read_agent_spec`` by the contract its ~15 bare call sites read it on, and
    ``project_agent_names`` in its own docstring. Either raise would abort
    whichever surface asked on exactly the hostile path the refusal handles --
    and ``project_agent_names`` runs on EVERY turn of a project-agent-bound
    session, with a caller-supplied path.
    """

    @staticmethod
    def _break_sel(monkeypatch):
        from kiro_crew import agent_discovery

        monkeypatch.setattr(agent_discovery, "is_sensitive_path", lambda _p: True)

        def _explode():
            raise OSError("SEL home is not writable")

        monkeypatch.setattr(agent_discovery, "_sel", _explode)
        return agent_discovery

    def test_reader_audit_failure_still_degrades_to_absent(self, tmp_path, monkeypatch, caplog):
        spec = tmp_path / "evil.json"
        spec.write_text(json.dumps({"name": "evil"}), encoding="utf-8")
        agent_discovery = self._break_sel(monkeypatch)

        with caplog.at_level("WARNING", logger="kiro_crew.agent_discovery"):
            result = agent_discovery._read_agent_spec(spec, operation="doctor", source="cli")

        # The spec is still REFUSED, so nothing unaudited is read; only the audit
        # ROW is lost, and that hole in the trail is operator-visible.
        assert result is None
        assert any("audit row lost" in r.message for r in caplog.records)

    def test_project_scan_audit_failure_still_yields_empty(self, tmp_path, monkeypatch, caplog):
        agent_discovery = self._break_sel(monkeypatch)

        with caplog.at_level("WARNING", logger="kiro_crew.agent_discovery"):
            result = agent_discovery.project_agent_names(tmp_path)

        assert result == frozenset()
        assert any("audit row lost" in r.message for r in caplog.records)


class TestSensitiveSymlinkGuard:
    """One representative surface proves the symlink guard flows through.

    The guard's own matrix lives with ``_read_agent_spec``; this pins that a
    migrated caller actually consults it (same shape as #5423's test).
    """

    @requires_symlinks
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

    @requires_symlinks
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


class TestDenialAttribution:
    """Sensitive-path denials name the surface that triggered the read."""

    @staticmethod
    def _denial_events(tmp_path, monkeypatch):
        """Return a sensitive spec and a spy collecting SEL denial fields.

        Symlink resolution itself is covered separately. This attribution test
        uses a regular file so it exercises the SEL event on hosts that cannot
        create symlinks (notably unelevated Windows shells).
        """
        from types import SimpleNamespace

        from kiro_crew import agent_discovery

        path = tmp_path / "protected.json"
        path.write_text(json.dumps({"name": "linked"}), encoding="utf-8")
        monkeypatch.setattr(agent_discovery, "is_sensitive_path", lambda p: str(path) in str(p))
        events: list[dict] = []
        monkeypatch.setattr(
            agent_discovery,
            "_sel",
            lambda: SimpleNamespace(log_api_access=lambda **kw: events.append(kw)),
        )
        return path, events

    def test_default_call_shape_emits_exactly_the_historical_event(self, tmp_path, monkeypatch):
        """The compatibility defaults preserve the pre-attribution event."""
        from kiro_crew.agent_discovery import _read_agent_spec

        link, events = self._denial_events(tmp_path, monkeypatch)

        assert _read_agent_spec(link) is None
        assert events == [
            {
                "caller": "agent_discovery",
                "operation": "list_agents",
                "outcome": "denied",
                "source": "list_agents",
                "resources": str(link.resolve()),
                "error": "sensitive path rejected",
            }
        ]

    def test_labelled_call_attributes_the_denial_to_that_surface(self, tmp_path, monkeypatch):
        from kiro_crew.agent_discovery import _read_agent_spec

        link, events = self._denial_events(tmp_path, monkeypatch)

        assert _read_agent_spec(link, operation="doctor", source="cli") is None
        assert len(events) == 1
        assert events[0]["operation"] == "doctor"
        assert events[0]["source"] == "cli"
        assert events[0]["caller"] == "agent_discovery"
        assert events[0]["outcome"] == "denied"

    def test_source_defaults_independently_of_operation(self, tmp_path, monkeypatch):
        """Supplying an operation must not corrupt the source vocabulary."""
        from kiro_crew.agent_discovery import _read_agent_spec

        link, events = self._denial_events(tmp_path, monkeypatch)

        assert _read_agent_spec(link, operation="doctor") is None
        assert events[0]["operation"] == "doctor"
        assert events[0]["source"] == "list_agents"


class TestProjectNamesDenialAttribution:
    """Sensitive-project-dir denials name the surface that asked (#6764).

    Same contract as :class:`TestDenialAttribution`, for the OTHER denial path
    in the module: ``project_agent_names`` refuses a sensitive project
    directory before any filesystem access, and the SEL row it emits must
    attribute the refusal to the request that triggered it rather than echoing
    the helper's own name into the ``source`` vocabulary.
    """

    @staticmethod
    def _denial_events(tmp_path, monkeypatch):
        """Return a sensitive project dir and a spy collecting SEL fields."""
        from types import SimpleNamespace

        from kiro_crew import agent_discovery

        project_dir = tmp_path / "protected-project"
        project_dir.mkdir()
        monkeypatch.setattr(
            agent_discovery, "is_sensitive_path", lambda p: str(project_dir) in str(p)
        )
        events: list[dict] = []
        monkeypatch.setattr(
            agent_discovery,
            "_sel",
            lambda: SimpleNamespace(log_api_access=lambda **kw: events.append(kw)),
        )
        return project_dir, events

    def test_default_call_shape_emits_exactly_the_historical_event(self, tmp_path, monkeypatch):
        """The compatibility defaults preserve the pre-attribution event."""
        from kiro_crew.agent_discovery import project_agent_names

        project_dir, events = self._denial_events(tmp_path, monkeypatch)

        assert project_agent_names(project_dir) == frozenset()
        assert events == [
            {
                "caller": "agent_discovery",
                "operation": "project_agent_names",
                "outcome": "denied",
                "source": "project_agent_names",
                "resources": str(project_dir),
                "error": "sensitive project dir rejected",
            }
        ]

    def test_labelled_call_attributes_the_denial_to_that_surface(self, tmp_path, monkeypatch):
        from kiro_crew.agent_discovery import project_agent_names

        project_dir, events = self._denial_events(tmp_path, monkeypatch)

        result = project_agent_names(
            project_dir, operation="api_kirocrew_agents", source="dashboard"
        )
        assert result == frozenset()
        assert len(events) == 1
        assert events[0]["operation"] == "api_kirocrew_agents"
        assert events[0]["source"] == "dashboard"
        assert events[0]["caller"] == "agent_discovery"
        assert events[0]["outcome"] == "denied"

    def test_source_defaults_independently_of_operation(self, tmp_path, monkeypatch):
        """Supplying an operation must not corrupt the source vocabulary."""
        from kiro_crew.agent_discovery import project_agent_names

        project_dir, events = self._denial_events(tmp_path, monkeypatch)

        assert project_agent_names(project_dir, operation="project_declares_agent") == frozenset()
        assert events[0]["operation"] == "project_declares_agent"
        assert events[0]["source"] == "project_agent_names"

    def test_warm_hop_forwards_the_labels_to_the_scan(self, tmp_path, monkeypatch):
        """The executor hop must not erase the caller's attribution."""
        import asyncio

        from kiro_crew.agent_discovery import warm_project_agent_names

        project_dir, events = self._denial_events(tmp_path, monkeypatch)

        asyncio.run(
            warm_project_agent_names(project_dir, operation="chat_turn", source="dashboard")
        )
        assert len(events) == 1
        assert events[0]["operation"] == "chat_turn"
        assert events[0]["source"] == "dashboard"

    def test_bare_warm_emits_the_wrapper_defaults(self, tmp_path, monkeypatch):
        """A forgotten future warm caller degrades to the wrapper's own truthful
        labels (operation names the warm, channel unknown) -- pinned so the
        default pair cannot drift unrecorded."""
        import asyncio

        from kiro_crew.agent_discovery import warm_project_agent_names

        project_dir, events = self._denial_events(tmp_path, monkeypatch)

        asyncio.run(warm_project_agent_names(project_dir))
        assert len(events) == 1
        assert events[0]["operation"] == "warm_project_agent_names"
        assert events[0]["source"] == "unknown"


# Exact direct-call inventory. A new caller must name its user-facing operation
# and interface channel (or ``unknown`` for a helper shared across interfaces).
# Forwarding helpers are pinned as forwarding rather than forced to use a fixed
# literal that would erase the caller's attribution.
_EXPECTED_CALL_SITE_LABELS: dict[str, list[tuple[str, str]]] = {
    # Two reads, deliberately labelled apart: the session-MCP translation resolves
    # the PROJECT checkout first (kiro-cli resolves --agent there before the user
    # level) and falls back to the user-level spec, so a refusal names which of the
    # two was refused rather than leaving the reader to guess.
    "kiro_crew/acp/session_mcp.py": [
        ("session_mcp_project_agent", "unknown"),
        ("session_mcp_servers", "unknown"),
    ],
    "kiro_crew/agent.py": [
        ("agent_spec_lookup", "unknown"),
        ("migrate_agent_specs", "unknown"),
    ],
    "kiro_crew/agent_discovery.py": [
        ("agent_skill_globs", "unknown"),
        ("forward:operation", "forward:source"),
        ("list_agents", "unknown"),
        ("list_agents", "unknown"),
        ("resolve_project_agent_name", "unknown"),
    ],
    "kiro_crew/cli_doctor.py": [("doctor", "cli"), ("doctor", "cli"), ("doctor", "cli")],
    "kiro_crew/config/loader.py": [("load_config", "unknown")],
    "kiro_crew/connections/mint.py": [
        ("connections_mint", "dashboard"),
        ("connections_mint", "dashboard"),
    ],
    "kiro_crew/connections/warm.py": [
        ("connections_warm_mint", "dashboard"),
        ("connections_warm_mint", "dashboard"),
    ],
    "kiro_crew/context.py": [("steering_resources", "unknown")],
    "kiro_crew/cron_script.py": [("cron_resolve_mcp_server", "cron")],
    "kiro_crew/dashboard/handlers/agents.py": [
        ("api_agent_detail", "dashboard"),
        ("api_agent_detail", "dashboard"),
        ("api_agents_sync", "dashboard"),
    ],
    "kiro_crew/dashboard/handlers/hooks.py": [("api_kiro_hooks", "dashboard")],
    "kiro_crew/dashboard/handlers/mcp.py": [
        ("api_mcp_active", "dashboard"),
        ("mcp_find_server_spec", "dashboard"),
        ("mcp_server_rows", "dashboard"),
        ("mcp_stub_eligibility", "dashboard"),
    ],
    "kiro_crew/mcp_discovery.py": [("mcp_discovery_agent_config", "unknown")],
    "kiro_crew/session.py": [
        ("forward:operation", "forward:source"),
        ("resolve_agent_model", "unknown"),
    ],
}


# Same contract for ``project_agent_names`` (#6764): its sensitive-project-dir
# denial carries operation/source, so every scan-reaching call site must name
# its user-facing operation and interface channel. ``warm_project_agent_names``
# is pinned as forwarding -- a fixed literal there would erase the attribution
# of whichever surface requested the warm. ``cached_project_agent_names`` is
# absent by design: it is a pure in-memory lookup that performs no filesystem
# work and can never reach the denial log line.
_EXPECTED_PROJECT_NAMES_CALL_SITE_LABELS: dict[str, list[tuple[str, str]]] = {
    "kiro_crew/agent_discovery.py": [("forward:operation", "forward:source")],
    "kiro_crew/config/loader.py": [("project_declares_agent", "unknown")],
    "kiro_crew/dashboard/handlers/agents.py": [("api_kirocrew_agents", "dashboard")],
}


# The warm wrapper's own callers are held to the same contract: the wrapper
# forwards to ``project_agent_names``, so an unlabelled warm caller would land
# every denial on the wrapper's defaults -- and the warm path is the
# highest-traffic route to the scan (a chat turn's ``slot.project`` reaches it
# every turn), so it is exactly where attribution matters most.
_EXPECTED_WARM_CALL_SITE_LABELS: dict[str, list[tuple[str, str]]] = {
    "kiro_crew/dashboard/chat_handlers.py": [
        ("api_chat", "dashboard"),
        ("api_chat_slot_agent", "dashboard"),
    ],
    "kiro_crew/dashboard/chat_runner.py": [("chat_turn", "unknown")],
    "kiro_crew/dashboard/handlers/side.py": [("side_panel", "dashboard")],
    "kiro_crew/spawn_warm.py": [("spawn_warm", "unknown")],
}


def _labelled_call_sites(target: str) -> dict[str, list[tuple[str | None, str | None]]]:
    """Return every *target* call site and its label pair.

    A site that hands the *target* off by REFERENCE counts too: handing it to
    ``asyncio.to_thread`` / ``functools.partial`` / an executor never produces
    a Call node named after the target, so matching only direct calls leaves
    every off-loop caller invisible to this ratchet. That is not hypothetical:
    one such site (an executor hop handing off ``_read_agent_spec``) really did
    sit behind the callee's defaults, recording its denials as an agent-listing
    cache warm. Any call that merely PASSES the target is therefore a site as
    well, and its labels are read from the handing-off call, which is where the
    forwarded kwargs are written. This applies to every entry in
    ``_RATCHET_INVENTORY``, not to any one callee.
    """
    src = Path(__file__).resolve().parent.parent / "src"
    sites: dict[str, list[tuple[str | None, str | None]]] = {}
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute) else ""
            )
            if name != target:
                # Not the callee itself -- but it is a site if it hands the
                # callee off to be invoked elsewhere (to_thread, partial, an
                # executor). Positional only: a callee is never passed by
                # keyword in these shapes.
                if not any(isinstance(arg, ast.Name) and arg.id == target for arg in node.args):
                    continue
            labels: dict[str, str | None] = {"operation": None, "source": None}
            for kw in node.keywords:
                if kw.arg not in labels:
                    continue
                if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    labels[kw.arg] = kw.value.value
                elif isinstance(kw.value, ast.Name) and kw.value.id == kw.arg:
                    labels[kw.arg] = f"forward:{kw.value.id}"
            sites.setdefault(path.relative_to(src).as_posix(), []).append(
                (labels["operation"], labels["source"])
            )
    return {
        path: sorted(pairs, key=lambda pair: tuple((item is not None, item or "") for item in pair))
        for path, pairs in sites.items()
    }


# The ratchet's coverage: every attribution-labelled helper in the module, with
# its exact call-site inventory. Extending attribution to a new helper means
# adding it here so its callers keep the two vocabularies separate too (#6764
# added ``project_agent_names``, mirroring #6722's ``_read_agent_spec``).
_RATCHET_INVENTORY: dict[str, dict[str, list[tuple[str, str]]]] = {
    "_read_agent_spec": _EXPECTED_CALL_SITE_LABELS,
    "project_agent_names": _EXPECTED_PROJECT_NAMES_CALL_SITE_LABELS,
    "warm_project_agent_names": _EXPECTED_WARM_CALL_SITE_LABELS,
}


class TestCallSiteLabelRatchet:
    """Every direct reader call is enumerated and explicitly attributed."""

    @pytest.mark.parametrize("target", sorted(_RATCHET_INVENTORY))
    def test_every_call_site_carries_the_expected_label(self, target):
        assert _labelled_call_sites(target) == _RATCHET_INVENTORY[target]

    @pytest.mark.parametrize("target", sorted(_RATCHET_INVENTORY))
    def test_no_call_site_is_silently_unlabelled(self, target):
        for path, pairs in _labelled_call_sites(target).items():
            for operation, source in pairs:
                assert (
                    operation is not None
                ), f"{path} calls {target} without an explicit operation label"
                assert source is not None, f"{path} calls {target} without an explicit source label"
