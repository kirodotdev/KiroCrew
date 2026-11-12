"""Broker overlays for crewmate files, and the worker session's lookup of them.

A crewmate's worker on KAS gets a gateway-routed stdio server through a broker
stub, so the gateway starts it with its ``env`` credentials. The overlay pass
rewrites each crewmate file under the identity ``crewmate:<id>`` into
``<overlay>/mates/``, and a worker session finds the stubs by its own record.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import requires_symlinks
from kiro_crew import agent_discovery, mate_config, mate_worker
from kiro_crew.agent_sdk import agent_specs
from kiro_crew.mcp_gateway import mate_overlays, rewriter, session_servers
from kiro_crew.mcp_gateway.hashing import expand_stub_flags
from kiro_crew.mcp_gateway.launch_approval import LaunchApprovals
from kiro_crew.mcp_gateway.rewriter import _WRAPPER_MARKER, env_sidecar_dir, env_sidecar_name

SERVER = "jira"
WORKER = mate_overlays.MATE_WORKER_AGENT


def _write(path: Path, spec: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(spec))
    return path


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An agents folder, a crewmate folder holding ``pixel``, and its owner record."""
    agents = tmp_path / "agents"
    mates = tmp_path / "mates" / "agents"
    _write(agents / "kirocrew.json", {"name": "kirocrew", "mcpServers": {}})
    _write(
        mates / "pixel.json",
        {
            "name": "pixel",
            "tools": ["*"],
            "mcpServers": {SERVER: {"command": sys.executable, "args": ["-V"]}},
        },
    )
    _write(mates / f"{WORKER}.json", {"name": WORKER, "mcpServers": {}})
    on_record = {"pixel"}
    monkeypatch.setattr(
        mate_config, "crewmate_file_on_record", lambda mate_id: mate_id in on_record
    )
    return SimpleNamespace(
        agents=agents,
        mates=mates,
        overlay=tmp_path / "crew" / "mcp-gateway" / "agents",
        tmp=tmp_path,
        on_record=on_record,
    )


def _rewrite(home, *, mates=True, pooling=True, approvals=None):
    return rewriter.rewrite_agents(
        source_dir=home.agents,
        overlay_dir=home.overlay,
        socket_path=home.tmp / "gw.sock",
        work_dir=home.tmp / "work",
        stub_servers=frozenset({SERVER}),
        pooling_enabled=pooling,
        approvals=approvals,
        mate_source_dir=home.mates if mates else None,
    )


def _mate_overlay(home, name="pixel") -> dict:
    return json.loads((mate_overlays.mate_overlay_dir(home.overlay) / f"{name}.json").read_text())


def _agent_flag(entry: dict) -> str:
    flags = expand_stub_flags(entry["args"])
    return flags[flags.index("--agent") + 1]


class TestTheRewritePass:
    def test_a_crewmate_server_gets_a_stub_under_the_crewmate_identity(self, home):
        results, target_env = _rewrite(home)
        entry = _mate_overlay(home)["mcpServers"][SERVER]
        assert entry[_WRAPPER_MARKER] is True
        assert _agent_flag(entry) == "crewmate:pixel"
        assert _mate_overlay(home)["name"] == "crewmate:pixel"
        assert results["mates/pixel.json"] == 1
        # The daemon is handed the target, or the stub would be rejected.
        assert any(k.startswith("KIROCREW_MCP_TARGET_JIRA") for k in target_env)

    def test_the_worker_and_conductor_specs_are_not_crewmates(self, home):
        _rewrite(home)
        listed = sorted(p.name for p in mate_overlays.mate_overlay_dir(home.overlay).iterdir())
        assert listed == ["pixel.json"]

    def test_the_conductor_spec_gets_its_own_overlay_by_name(self, home):
        """A crewmate's chat runs the stock conductor spec and looks its stubs up by name."""
        conductor = mate_worker.MATE_CONDUCTOR_AGENT
        launch = {"command": sys.executable, "args": ["-V"]}
        _write(
            home.mates / f"{conductor}.json",
            {"name": conductor, "tools": ["*"], "mcpServers": {SERVER: launch}},
        )
        # A same-named file in ~/.kiro/agents is not the spec the chat runs.
        _write(home.agents / f"{conductor}.json", {"name": conductor, "mcpServers": {}})
        _rewrite(home)
        path = home.overlay / f"{conductor}.json"
        assert path.is_file()
        servers = json.loads(path.read_text())["mcpServers"]
        assert SERVER in servers
        assert _agent_flag(servers[SERVER]) == conductor
        injected = session_servers.pooled_session_servers(home.overlay, conductor)
        assert [e["name"] for e in injected] == [SERVER]

    def test_the_conductor_files_own_name_is_not_its_identity(self, home):
        """The crewmate folder is outside the sandbox seal, so the file cannot pick its identity."""
        conductor = mate_worker.MATE_CONDUCTOR_AGENT
        launch = {"command": sys.executable, "args": ["-V"]}
        _write(
            home.mates / f"{conductor}.json",
            {"name": "kirocrew", "tools": ["*"], "mcpServers": {SERVER: launch}},
        )
        _rewrite(home)
        path = home.overlay / f"{conductor}.json"
        assert path.is_file()
        entry = json.loads(path.read_text())["mcpServers"][SERVER]
        assert _agent_flag(entry) == conductor

    def test_a_credential_env_lands_in_a_sidecar_named_for_the_crewmate(self, home):
        _write(
            home.mates / "pixel.json",
            {
                "name": "pixel",
                "tools": ["*"],
                "mcpServers": {SERVER: {"command": sys.executable, "env": {"API_TOKEN": "t"}}},
            },
        )
        _rewrite(home, pooling=False)
        sidecar = env_sidecar_dir(home.overlay) / env_sidecar_name("crewmate:pixel", SERVER)
        assert json.loads(sidecar.read_text()) == {"API_TOKEN": "t"}
        flags = expand_stub_flags(_mate_overlay(home)["mcpServers"][SERVER]["args"])
        assert flags[flags.index("--env-file") + 1] == str(sidecar)

    def test_a_file_not_on_the_crewmate_record_gets_no_overlay(self, home):
        home.on_record.clear()
        _rewrite(home)
        assert not (mate_overlays.mate_overlay_dir(home.overlay) / "pixel.json").exists()

    def test_a_record_that_cannot_be_read_writes_no_crewmate_overlay(self, home, monkeypatch):
        def _unreadable(_mate_id):
            raise mate_config.MateConfigError("owner record unreadable")

        monkeypatch.setattr(mate_config, "crewmate_file_on_record", _unreadable)
        _rewrite(home)
        assert not (mate_overlays.mate_overlay_dir(home.overlay) / "pixel.json").exists()

    @pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
    def test_a_symlinked_crewmate_file_gets_no_overlay(self, home):
        real = _write(
            home.tmp / "elsewhere.json", json.loads((home.mates / "pixel.json").read_text())
        )
        (home.mates / "pixel.json").unlink()
        (home.mates / "pixel.json").symlink_to(real)
        _rewrite(home)
        assert not (mate_overlays.mate_overlay_dir(home.overlay) / "pixel.json").exists()

    def test_an_agent_declaring_the_crewmate_identity_blocks_the_crewmate(self, home):
        _write(home.agents / "odd.json", {"name": "crewmate:pixel", "mcpServers": {}})
        _rewrite(home)
        assert not (mate_overlays.mate_overlay_dir(home.overlay) / "pixel.json").exists()
        # The prefix is reserved: the agent gets no overlay either, so no stub of
        # an ordinary session carries an identity governed as a crewmate's.
        assert not (home.overlay / "odd.json").exists()

    def test_an_agent_with_the_reserved_prefix_gets_no_stub(self, home):
        _write(
            home.agents / "odd.json",
            {
                "name": "crewmate:nobody",
                "tools": ["*"],
                "mcpServers": {SERVER: {"command": sys.executable, "args": ["-V"]}},
            },
        )
        results, _target_env = _rewrite(home)
        assert not (home.overlay / "odd.json").exists()
        assert "odd.json" not in results
        # The crewmate the prefix does not name is unaffected.
        assert _agent_flag(_mate_overlay(home)["mcpServers"][SERVER]) == "crewmate:pixel"

    def test_a_kept_overlay_with_the_reserved_prefix_is_not_kept(self, home, monkeypatch):
        source = _write(home.agents / "odd.json", {"name": "odd", "mcpServers": {}})
        # An overlay from before the prefix was reserved, then a source that
        # cannot be read: the keep path would serve it without reading a name.
        _write(home.overlay / "odd.json", {"name": "crewmate:nobody", "mcpServers": {}})
        real_read = agent_discovery._read_spec_bytes

        def _unreadable(path: Path) -> bytes:
            if Path(path).name == source.name:
                raise PermissionError("unreadable")
            return real_read(path)

        monkeypatch.setattr(agent_discovery, "_read_spec_bytes", _unreadable)
        approvals = LaunchApprovals()
        _rewrite(home, approvals=approvals)
        assert not (home.overlay / "odd.json").exists()
        # The source was still not read, so its launches are not in the live set.
        assert approvals.live_incomplete is True

    def test_the_crewmate_launch_needs_the_same_approval(self, home):
        approvals = LaunchApprovals()
        _results, target_env = _rewrite(home, approvals=approvals)
        entry = _mate_overlay(home)["mcpServers"][SERVER]
        assert _WRAPPER_MARKER not in entry
        assert "JIRA" in approvals.refused
        assert not any(k.startswith("KIROCREW_MCP_TARGET_JIRA") for k in target_env)

    def test_a_pass_without_the_crewmate_folder_prunes_crewmate_overlays(self, home):
        _rewrite(home)
        _rewrite(home, mates=False)
        assert list(mate_overlays.mate_overlay_dir(home.overlay).glob("*.json")) == []

    def test_a_deleted_crewmate_file_is_pruned(self, home):
        _rewrite(home)
        (home.mates / "pixel.json").unlink()
        _rewrite(home)
        assert not (mate_overlays.mate_overlay_dir(home.overlay) / "pixel.json").exists()

    def test_an_unchanged_pass_serves_the_crewmate_target_from_cache(self, home):
        _first, env = _rewrite(home)
        _again, cached = _rewrite(home)
        assert cached == env
        overlay = mate_overlays.mate_overlay_dir(home.overlay) / "pixel.json"
        overlay.unlink()
        _rewrite(home)
        assert overlay.exists()

    def test_a_stray_file_in_the_crewmate_overlays_is_pruned_on_a_cache_hit(self, home):
        _rewrite(home)
        stray = _write(mate_overlays.mate_overlay_dir(home.overlay) / "stray.json", {"name": "x"})
        _rewrite(home)
        assert not stray.exists()


def _worker_session(monkeypatch, home, *, merged_servers, member="m-1", base_servers=None):
    """Bind one worker session to crewmate ``pixel`` with the given merged spec."""
    execution = SimpleNamespace(selection_kind="template", template_id=WORKER, member_id=member)
    monkeypatch.setattr("kiro_crew.execution_context.read_session_execution", lambda key: execution)
    monkeypatch.setattr(mate_worker, "mate_file_for_member", lambda member_id: "pixel")
    base = {"name": WORKER, "mcpServers": base_servers or {}}
    monkeypatch.setattr(agent_specs, "load_agent_spec", lambda d, a: dict(base))
    monkeypatch.setattr(
        mate_worker,
        "worker_session_spec",
        lambda b, key: {"name": WORKER, "mcpServers": merged_servers},
    )


class TestTheWorkerLookup:
    def test_the_worker_gets_its_crewmates_stub_without_auto_approval(self, home, monkeypatch):
        _rewrite(home)
        governed = {
            "command": sys.executable,
            "autoApprove": ["read"],
            "disabledTools": ["write"],
        }
        _worker_session(monkeypatch, home, merged_servers={SERVER: governed})
        spec = mate_overlays.worker_overlay_spec(home.overlay, "chat-1")
        assert list(spec["mcpServers"]) == [SERVER]
        assert "autoApprove" not in spec["mcpServers"][SERVER]
        assert spec["mcpServers"][SERVER]["disabledTools"] == ["write"]
        injected = session_servers.pooled_session_servers(
            home.overlay, WORKER, kas_worker_session_key="chat-1"
        )
        assert [e["name"] for e in injected] == [SERVER]

    def test_an_auto_approve_in_the_crewmate_file_never_reaches_the_worker(self, home, monkeypatch):
        _write(
            home.mates / "pixel.json",
            {
                "name": "pixel",
                "tools": ["*"],
                "mcpServers": {
                    SERVER: {"command": sys.executable, "args": ["-V"], "autoApprove": ["*"]}
                },
            },
        )
        _rewrite(home)
        # The overlay keeps the file's grant, so the worker lookup must drop it.
        assert _mate_overlay(home)["mcpServers"][SERVER]["autoApprove"] == ["*"]
        _worker_session(monkeypatch, home, merged_servers={SERVER: {"command": sys.executable}})
        spec = mate_overlays.worker_overlay_spec(home.overlay, "chat-1")
        assert "autoApprove" not in spec["mcpServers"][SERVER]
        injected = session_servers.pooled_session_servers(
            home.overlay, WORKER, kas_worker_session_key="chat-1"
        )
        assert [e["name"] for e in injected] == [SERVER]
        assert all("autoApprove" not in json.dumps(e) for e in injected)

    def test_without_a_kas_session_key_the_lookup_is_the_by_name_one(self, home, monkeypatch):
        """Every host but KAS passes no key, and keeps the lookup it had on main."""
        _rewrite(home)
        _worker_session(monkeypatch, home, merged_servers={SERVER: {"command": "x"}})
        # No leftover copy under the shared name: nothing, as on main.
        assert session_servers.pooled_session_servers(home.overlay, WORKER) == []
        # A leftover ``~/.kiro/agents/kirocrew-mate-worker.json`` overlay still
        # answers the by-name lookup, as on main.
        _write(home.overlay / f"{WORKER}.json", _mate_overlay(home) | {"name": WORKER})
        assert [
            e["name"] for e in session_servers.pooled_session_servers(home.overlay, WORKER)
        ] == [SERVER]
        assert session_servers.injection_server_names(home.overlay, WORKER) == frozenset({SERVER})

    def test_a_server_the_merged_spec_drops_or_mutes_is_not_injected(self, home, monkeypatch):
        _rewrite(home)
        _worker_session(monkeypatch, home, merged_servers={})
        assert mate_overlays.worker_overlay_spec(home.overlay, "chat-1")["mcpServers"] == {}
        _worker_session(
            monkeypatch, home, merged_servers={SERVER: {"command": "x", "disabled": True}}
        )
        assert mate_overlays.worker_overlay_spec(home.overlay, "chat-1")["mcpServers"] == {}

    def test_a_stub_whose_entry_became_registry_governed_is_not_injected(self, home, monkeypatch):
        """The overlay holds the last pass's stub; the entry was retyped since."""
        _rewrite(home)
        _worker_session(
            monkeypatch, home, merged_servers={SERVER: {"command": "x", "type": "registry"}}
        )
        assert mate_overlays.worker_overlay_spec(home.overlay, "chat-1")["mcpServers"] == {}
        assert (
            session_servers.pooled_session_servers(
                home.overlay, WORKER, kas_worker_session_key="chat-1"
            )
            == []
        )

    def test_a_base_server_of_the_same_name_wins_over_the_crewmate_stub(self, home, monkeypatch):
        _rewrite(home)
        _worker_session(
            monkeypatch,
            home,
            merged_servers={SERVER: {"command": "base"}},
            base_servers={SERVER: {"command": "base"}},
        )
        assert mate_overlays.worker_overlay_spec(home.overlay, "chat-1")["mcpServers"] == {}

    def test_another_session_kind_gets_nothing(self, home, monkeypatch):
        _rewrite(home)
        _worker_session(monkeypatch, home, merged_servers={SERVER: {"command": "x"}})
        monkeypatch.setattr(
            "kiro_crew.execution_context.read_session_execution",
            lambda key: SimpleNamespace(selection_kind="member", template_id=WORKER, member_id="m"),
        )
        assert mate_overlays.worker_overlay_spec(home.overlay, "chat-1") is None

    def test_an_overlay_with_another_identity_is_refused(self, home, monkeypatch):
        _rewrite(home)
        path = mate_overlays.mate_overlay_dir(home.overlay) / "pixel.json"
        _write(path, _mate_overlay(home) | {"name": "kirocrew"})
        _worker_session(monkeypatch, home, merged_servers={SERVER: {"command": "x"}})
        assert mate_overlays.worker_overlay_spec(home.overlay, "chat-1") is None

    @pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
    def test_a_symlinked_overlay_is_refused(self, home, monkeypatch):
        _rewrite(home)
        path = mate_overlays.mate_overlay_dir(home.overlay) / "pixel.json"
        real = _write(home.tmp / "planted.json", _mate_overlay(home))
        path.unlink()
        path.symlink_to(real)
        _worker_session(monkeypatch, home, merged_servers={SERVER: {"command": "x"}})
        assert mate_overlays.worker_overlay_spec(home.overlay, "chat-1") is None

    @pytest.mark.parametrize("bound", ["../agents/kirocrew", "a\\b", ".."])
    def test_a_binding_that_names_a_path_is_refused(self, home, monkeypatch, bound):
        _rewrite(home)
        _worker_session(monkeypatch, home, merged_servers={SERVER: {"command": "x"}})
        monkeypatch.setattr(mate_worker, "mate_file_for_member", lambda member_id: bound)
        assert mate_overlays.worker_overlay_spec(home.overlay, "chat-1") is None

    def test_an_unreadable_worker_kit_injects_nothing(self, home, monkeypatch):
        _rewrite(home)
        _worker_session(monkeypatch, home, merged_servers={SERVER: {"command": "x"}})

        def _refuse(_base, _key):
            raise mate_worker.WorkerKitUnreadable("limits unreadable")

        monkeypatch.setattr(mate_worker, "worker_session_spec", _refuse)
        assert mate_overlays.worker_overlay_spec(home.overlay, "chat-1") is None


class TestTheSessionKeyReachesTheLookupOnKasOnly:
    @pytest.mark.parametrize(("backend", "expected"), [("kas", "chat-1"), ("kiro", None)])
    def test_the_pooled_hop(self, monkeypatch, tmp_path, backend, expected):
        from kiro_crew.acp import runtime as runtime_mod

        seen: dict = {}

        def _pooled(overlay, agent, **kw):
            seen.update(kw)
            return []

        monkeypatch.setattr(runtime_mod, "pooled_session_servers", _pooled)
        monkeypatch.setattr(runtime_mod, "_ref_spec_snapshot", lambda *a: None)
        runtime_mod._pooled_session_servers_and_ref_spec(
            tmp_path, WORKER, backend, tmp_path, None, "chat-1"
        )
        assert seen.get("kas_worker_session_key") == expected


class TestTheKasWorkerStubSetIsOneLookup:
    def test_the_names_are_the_injected_entries_on_kas_only(self):
        from kiro_crew.acp.runtime_session_setup import _kas_worker_stub_names

        pooled = [{"name": SERVER}, {"name": "other"}]
        assert _kas_worker_stub_names("kas", WORKER, "chat-1", pooled) == frozenset(
            {SERVER, "other"}
        )
        assert _kas_worker_stub_names("kiro", WORKER, "chat-1", pooled) is None
        assert _kas_worker_stub_names("kas", "kirocrew", "chat-1", pooled) is None
        assert _kas_worker_stub_names("kas", WORKER, "", pooled) is None

    @pytest.mark.asyncio
    async def test_the_runtime_hands_the_names_down_only_when_it_has_them(self):
        from kiro_crew.acp import runtime_session_setup as setup_mod
        from kiro_crew.acp.harness import SessionExtras

        seen: list[dict] = []

        class _Harness:
            async def session_extras(self, agent, **kw):
                seen.append(kw)
                return SessionExtras()

        fake = SimpleNamespace(
            _harness=_Harness(),
            _work_dir=None,
            _mcp_gateway_overlay=None,
            _refuse_if_loader_unreachable=lambda agent, custom: None,
        )
        await setup_mod._kas_custom_agents(fake, "a", session_key="chat-1")
        await setup_mod._kas_custom_agents(
            fake, WORKER, session_key="chat-1", worker_stub_names=frozenset({SERVER})
        )
        assert "worker_stub_names" not in seen[0]
        assert seen[0]["session_key"] == "chat-1"
        assert seen[1]["worker_stub_names"] == frozenset({SERVER})

    @pytest.mark.asyncio
    async def test_kas_subtracts_the_handed_names_for_the_worker(self, monkeypatch, tmp_path):
        from kiro_crew import agent as agent_mod
        from kiro_crew.acp import kas_agents as kas_agents_mod
        from kiro_crew.acp.harness import harness_for
        from kiro_crew.acp.harness import kas as kas_mod
        from kiro_crew.config import paths as paths_mod

        monkeypatch.setattr(agent_mod, "require_fork_governance", lambda agent, work_dir: None)
        monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda agent: None)
        monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: tmp_path)
        monkeypatch.setattr(paths_mod, "mate_agents_dir", lambda: tmp_path / "no-mates")
        monkeypatch.setattr(
            session_servers,
            "injection_server_names",
            lambda overlay, agent, **kw: frozenset({"by-name"}),
        )
        monkeypatch.setattr(kas_mod, "mate_worker_projection", lambda spec, d, key: spec)
        for name in ("a", WORKER):
            _write(tmp_path / f"{name}.json", {"name": name, "prompt": "p", "tools": []})
        subtracted: list[frozenset] = []

        def _build(d, a, spec, *, stub_server_names, member_dispatch, **_kw):
            subtracted.append(frozenset(stub_server_names))
            return [{"name": a}]

        monkeypatch.setattr(kas_agents_mod, "build_kas_custom_agents", _build)
        kas = harness_for("kas")
        handed = frozenset({SERVER})
        await kas.session_extras(WORKER, work_dir=str(tmp_path), worker_stub_names=handed)
        await kas.session_extras(WORKER, work_dir=str(tmp_path))
        await kas.session_extras("a", work_dir=str(tmp_path), worker_stub_names=handed)
        assert subtracted == [handed, frozenset({"by-name"}), frozenset({"by-name"})]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("mounted", "withdrawn"),
        [
            ({SERVER: {"command": "x"}}, frozenset()),
            ({SERVER: {"command": "x", "disabled": True}}, frozenset({SERVER})),
            ({SERVER: {"command": "x", "type": "registry"}}, frozenset({SERVER})),
            ({}, frozenset({SERVER})),
        ],
    )
    async def test_a_server_muted_after_the_stubs_were_picked_is_withdrawn(
        self, monkeypatch, tmp_path, mounted, withdrawn
    ):
        """The stubs are picked from one read of the worker spec and the payload
        from a later one; a mute saved in between is the later read's."""
        from kiro_crew import agent as agent_mod
        from kiro_crew.acp import kas_agents as kas_agents_mod
        from kiro_crew.acp.harness import harness_for
        from kiro_crew.acp.harness import kas as kas_mod
        from kiro_crew.config import paths as paths_mod

        monkeypatch.setattr(agent_mod, "require_fork_governance", lambda agent, work_dir: None)
        monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda agent: None)
        monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: tmp_path)
        monkeypatch.setattr(paths_mod, "mate_agents_dir", lambda: tmp_path / "no-mates")
        monkeypatch.setattr(
            kas_mod,
            "mate_worker_projection",
            lambda spec, d, key: {**spec, "mcpServers": mounted},
        )
        _write(tmp_path / f"{WORKER}.json", {"name": WORKER, "prompt": "p", "tools": []})
        monkeypatch.setattr(
            kas_agents_mod, "build_kas_custom_agents", lambda d, a, spec, **_kw: [{"name": a}]
        )
        extras = await harness_for("kas").session_extras(
            WORKER, work_dir=str(tmp_path), worker_stub_names=frozenset({SERVER})
        )
        assert extras.withdrawn_stub_names == withdrawn

    @pytest.mark.parametrize(
        ("entry", "withdrawn"),
        [
            ({"command": sys.executable}, frozenset()),
            ({"command": sys.executable, "disabled": True}, frozenset({SERVER})),
            ({"command": sys.executable, "type": "registry"}, frozenset({SERVER})),
            (None, frozenset({SERVER})),
        ],
    )
    def test_the_real_worker_merge_carries_the_mute_to_the_withdrawal(self, entry, withdrawn):
        from kiro_crew.acp.harness.kas import withdrawn_worker_stubs

        base = {"name": WORKER, "prompt": "p", "mcpServers": {}}
        mate = {"name": "pixel", "mcpServers": {} if entry is None else {SERVER: entry}}
        merged = mate_worker.merge_worker_spec(base, mate_worker.worker_additions(base, mate))
        assert withdrawn_worker_stubs(merged, frozenset({SERVER})) == withdrawn

    def test_a_withdrawn_stub_leaves_the_session_array(self):
        from kiro_crew.acp.runtime_session_setup import _drop_withdrawn_stubs

        servers = [{"name": SERVER}, {"name": "other"}]
        assert _drop_withdrawn_stubs(servers, frozenset()) is servers
        assert _drop_withdrawn_stubs(servers, frozenset({SERVER})) == [{"name": "other"}]


class TestFailClosedPaths:
    def test_an_unknown_kept_identity_set_writes_no_crewmate_overlay(self, home):
        published: list[Path] = []
        out = mate_overlays.rewrite_mate_overlays(
            mate_overlays.MateSources((home.mates / "pixel.json",)),
            overlay_dir=home.overlay,
            taken_identities=None,
            publish=lambda spec, target: published.append(target) or 1,
        )
        assert published == [] and out.incomplete and out.written == set()

    def test_an_unreadable_kept_agent_overlay_reads_as_unknown(self, home):
        home.overlay.mkdir(parents=True)
        (home.overlay / "bad.json").write_text("{not json")
        assert mate_overlays.kept_identities(home.overlay, ["bad.json"]) is None
        _write(home.overlay / "noname.json", {"mcpServers": {}})
        assert mate_overlays.kept_identities(home.overlay, ["noname.json"]) is None
        assert mate_overlays.kept_identities(home.overlay, ["gone.json"]) == set()

    def test_a_kept_agent_overlay_declaring_the_identity_blocks_the_crewmate(self, home):
        _write(home.overlay / "odd.json", {"name": "crewmate:pixel", "mcpServers": {}})
        kept = mate_overlays.kept_identities(home.overlay, ["odd.json"])
        assert kept == {"crewmate:pixel"}
        published: list[Path] = []
        out = mate_overlays.rewrite_mate_overlays(
            mate_overlays.MateSources((home.mates / "pixel.json",)),
            overlay_dir=home.overlay,
            taken_identities=kept,
            publish=lambda spec, target: published.append(target) or 1,
        )
        assert published == [] and out.written == set()

    @pytest.mark.parametrize("fault", ["record", "file", "listing"])
    def test_an_incomplete_pass_is_uncached_and_retires_no_launch(self, home, monkeypatch, fault):
        from kiro_crew import agent_spec_format

        if fault == "record":

            def _record(_mate_id):
                raise mate_config.MateConfigError("owner record unreadable")

            monkeypatch.setattr(mate_config, "crewmate_file_on_record", _record)
        elif fault == "file":
            real_read = agent_discovery.read_agent_spec_strict

            def _read(path, **kw):
                if Path(path).parent == home.mates:
                    raise OSError("busy")
                return real_read(path, **kw)

            monkeypatch.setattr(agent_discovery, "read_agent_spec_strict", _read)
        else:

            def _list(_d):
                raise OSError("busy")

            monkeypatch.setattr(agent_spec_format, "iter_agent_spec_files_strict", _list)
        approvals = LaunchApprovals()
        _rewrite(home, approvals=approvals)
        assert not (mate_overlays.mate_overlay_dir(home.overlay) / "pixel.json").exists()
        assert not (home.overlay / rewriter._FINGERPRINT_NAME).exists()
        assert approvals.live_incomplete is True

    def test_a_complete_pass_is_cached(self, home):
        approvals = LaunchApprovals()
        _rewrite(home, approvals=approvals)
        assert (home.overlay / rewriter._FINGERPRINT_NAME).exists()
        assert approvals.live_incomplete is False

    def test_the_gateway_start_passes_the_crewmate_folder(self):
        from kiro_crew.config import paths
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.mcp_gateway.launch_resolve import rewrite_kwargs

        kwargs = rewrite_kwargs(KiroCrewConfig(), frozenset())
        assert kwargs["mate_source_dir"] == paths.mate_agents_dir()

    def test_a_failed_overlay_write_leaves_no_crewmate_sidecar(self, home, monkeypatch):
        spec = {
            "name": "pixel",
            "tools": ["*"],
            "mcpServers": {SERVER: {"command": sys.executable}},
        }
        spec["mcpServers"][SERVER]["env"] = {"API_TOKEN": "t"}
        _write(home.mates / "pixel.json", spec)
        _rewrite(home, pooling=False)
        sidecar = env_sidecar_dir(home.overlay) / env_sidecar_name("crewmate:pixel", SERVER)
        assert sidecar.exists()
        spec["mcpServers"][SERVER]["env"] = {"API_TOKEN": "rotated"}
        _write(home.mates / "pixel.json", spec)
        real_write = rewriter.atomic_write

        def _write_fails_for_crewmates(target, *a, **kw):
            if Path(target).parent.name == mate_overlays.MATE_OVERLAY_SUBDIR:
                raise OSError("disk full")
            return real_write(target, *a, **kw)

        monkeypatch.setattr(rewriter, "atomic_write", _write_fails_for_crewmates)
        _rewrite(home, pooling=False)
        assert not (mate_overlays.mate_overlay_dir(home.overlay) / "pixel.json").exists()
        assert not sidecar.exists()
        assert not (home.overlay / rewriter._FINGERPRINT_NAME).exists()


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
class TestALinkedCrewmateOverlayFolder:
    def _plant(self, home) -> Path:
        import shutil

        elsewhere = home.tmp / "elsewhere"
        elsewhere.mkdir()
        _write(elsewhere / "keep.json", {"name": "keep"})
        mates = mate_overlays.mate_overlay_dir(home.overlay)
        if mates.exists():
            shutil.rmtree(mates)
        home.overlay.mkdir(parents=True, exist_ok=True)
        mates.symlink_to(elsewhere, target_is_directory=True)
        return elsewhere

    def test_a_pass_writes_and_prunes_nothing_through_it(self, home):
        elsewhere = self._plant(home)
        _rewrite(home)
        assert sorted(p.name for p in elsewhere.iterdir()) == ["keep.json"]
        assert not (home.overlay / rewriter._FINGERPRINT_NAME).exists()

    def test_a_cached_pass_serves_and_prunes_nothing_through_it(self, home):
        _rewrite(home)
        elsewhere = self._plant(home)
        _rewrite(home)
        assert sorted(p.name for p in elsewhere.iterdir()) == ["keep.json"]

    def test_a_pass_without_crewmates_prunes_nothing_through_it(self, home):
        elsewhere = self._plant(home)
        _rewrite(home, mates=False)
        assert sorted(p.name for p in elsewhere.iterdir()) == ["keep.json"]

    def test_a_worker_reads_no_stub_through_it(self, home, monkeypatch):
        _rewrite(home)
        overlay = _mate_overlay(home)
        elsewhere = self._plant(home)
        _write(elsewhere / "pixel.json", overlay)
        _worker_session(monkeypatch, home, merged_servers={SERVER: {"command": "x"}})
        assert mate_overlays.worker_overlay_spec(home.overlay, "chat-1") is None


class TestAnEditedCrewmateFile:
    def test_it_runs_on_its_last_approved_stub_until_the_next_pass(self, home, monkeypatch):
        """Pinned staleness, the same an ``~/.kiro/agents`` spec has (mcp.md)."""
        _rewrite(home)
        before = _mate_overlay(home)["mcpServers"][SERVER]["args"]
        _write(
            home.mates / "pixel.json",
            {
                "name": "pixel",
                "tools": ["*"],
                "mcpServers": {SERVER: {"command": sys.executable, "args": ["-c"]}},
            },
        )
        _worker_session(monkeypatch, home, merged_servers={SERVER: {"command": sys.executable}})
        spec = mate_overlays.worker_overlay_spec(home.overlay, "chat-1")
        assert spec["mcpServers"][SERVER]["args"] == before
        _rewrite(home)
        assert _mate_overlay(home)["mcpServers"][SERVER]["args"] != before


class TestAppCallbackGovernance:
    @pytest.fixture
    def profiles(self, tmp_path, monkeypatch):
        import kiro_crew.platform.governance_profiles as gp

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        folder = tmp_path / "profiles"
        folder.mkdir()
        monkeypatch.setattr(gp, "_PROFILES_DIR", folder)

        def _bind(task: str) -> None:
            (folder / "scoped.json").write_text(
                json.dumps(
                    {
                        "name": "scoped",
                        "bind": {"type": "task", "id": task},
                        "mcp": {"mode": "deny", "deny": ["@srv/draw"]},
                    }
                ),
                encoding="utf-8",
            )
            gp.reset_store()

        yield _bind
        gp.reset_store()

    def test_governance_names(self):
        assert mate_overlays.governance_agents("kirocrew") == ("kirocrew",)
        assert mate_overlays.governance_agents("") == ("",)
        assert mate_overlays.governance_agents("crewmate:pixel") == (WORKER, "crewmate:pixel")
        for bad in ("crewmate:", "crewmate:..", "crewmate:a/b", "crewmate:a\\b"):
            assert mate_overlays.governance_agents(bad) is None

    @pytest.mark.parametrize("bound", [WORKER, "crewmate:pixel"])
    def test_the_worker_or_identity_profile_governs_a_crewmate_callback(self, profiles, bound):
        from kiro_crew.mcp_gateway.app_call import _governance_denial

        profiles(bound)
        denied = _governance_denial("srv", "draw", "dashboard:s", agent="crewmate:pixel")
        assert denied and denied.startswith("blocked by governance policy")
        # The control: another agent is not held to that profile.
        assert _governance_denial("srv", "draw", "dashboard:s", agent="kirocrew") is None

    def test_the_worker_task_profile_wins_over_the_surface_for_a_crewmate_callback(
        self, profiles, tmp_path
    ):
        """The identity adds only its own task bind, never a second surface fallback."""
        import kiro_crew.platform.governance_profiles as gp
        from kiro_crew.mcp_gateway.app_call import _governance_denial

        def _profile(stem: str, bind: dict, deny: list[str]) -> None:
            (tmp_path / "profiles" / f"{stem}.json").write_text(
                json.dumps(
                    {
                        "name": stem,
                        "bind": bind,
                        "mcp": {"mode": "deny", "deny": deny},
                    }
                ),
                encoding="utf-8",
            )

        surface = gp._infer_surface("dashboard:s")
        _profile("surface", {"type": "surface", "id": surface}, ["@srv/draw"])
        _profile("worker", {"type": "task", "id": WORKER}, ["@srv/other"])
        gp.reset_store()
        assert _governance_denial("srv", "draw", "dashboard:s", agent="crewmate:pixel") is None
        # The control: an agent with no task profile is held to the surface one.
        denied = _governance_denial("srv", "draw", "dashboard:s", agent="kirocrew")
        assert denied and denied.startswith("blocked by governance policy")

    def test_no_profile_leaves_a_crewmate_callback_to_the_ceiling(self, profiles):
        from kiro_crew.mcp_gateway.app_call import _governance_denial

        profiles("someone-else")
        assert _governance_denial("srv", "draw", "dashboard:s", agent="crewmate:pixel") is None

    def test_an_identity_naming_no_file_denies(self, profiles):
        from kiro_crew.mcp_gateway.app_call import _governance_denial

        profiles("someone-else")
        denied = _governance_denial("srv", "draw", "dashboard:s", agent="crewmate:../x")
        assert denied == "cannot resolve the calling session's agent"


def _credentialed_pixel(home) -> Path:
    """Give ``pixel`` a credential env, so a sidecar shows what the pass wrote."""
    spec = {"name": "pixel", "tools": ["*"], "mcpServers": {SERVER: {"command": sys.executable}}}
    spec["mcpServers"][SERVER]["env"] = {"API_TOKEN": "t"}
    _write(home.mates / "pixel.json", spec)
    return env_sidecar_dir(home.overlay) / env_sidecar_name("crewmate:pixel", SERVER)


class TestAnAgentOfTheSameName:
    @pytest.mark.parametrize("file_name", ["pixel.json", "other.json"])
    def test_an_agent_named_like_the_crewmate_file_blocks_its_overlay_and_sidecar(
        self, home, file_name
    ):
        """``mate_file_for_member`` refuses that binding, so nothing may be written."""
        sidecar = _credentialed_pixel(home)
        _write(home.agents / file_name, {"name": "pixel", "mcpServers": {}})
        approvals = LaunchApprovals()
        _rewrite(home, pooling=False, approvals=approvals)
        assert not (mate_overlays.mate_overlay_dir(home.overlay) / "pixel.json").exists()
        assert not sidecar.exists()
        # A settled refusal, not a doubt: the pass is cached.
        assert (home.overlay / rewriter._FINGERPRINT_NAME).exists()
        assert approvals.live_incomplete is False

    def test_an_agents_folder_that_cannot_be_checked_writes_nothing(self, home, monkeypatch):
        def _unscannable(_d, _agent_id):
            raise OSError("busy")

        monkeypatch.setattr(agent_specs, "agent_spec_absent", _unscannable)
        approvals = LaunchApprovals()
        _rewrite(home, approvals=approvals)
        assert not (mate_overlays.mate_overlay_dir(home.overlay) / "pixel.json").exists()
        assert not (home.overlay / rewriter._FINGERPRINT_NAME).exists()
        assert approvals.live_incomplete is True


@requires_symlinks
class TestSkippedCrewmateSourcesReadAsAbsent:
    """A linked folder or file, or an off-record file, is absent, not a doubt (mcp.md)."""

    @pytest.mark.parametrize("case", ["folder", "file", "off-record"])
    def test_no_overlay_and_the_pass_stays_complete(self, home, case):
        if case == "folder":
            real = home.tmp / "real-mates"
            home.mates.rename(real)
            home.mates.symlink_to(real, target_is_directory=True)
        elif case == "file":
            real_file = _write(
                home.tmp / "elsewhere.json", json.loads((home.mates / "pixel.json").read_text())
            )
            (home.mates / "pixel.json").unlink()
            (home.mates / "pixel.json").symlink_to(real_file)
        else:
            home.on_record.clear()
        sources = mate_overlays.list_mate_sources(home.mates)
        assert sources.files == () and sources.complete is True
        approvals = LaunchApprovals()
        _rewrite(home, approvals=approvals)
        assert not (mate_overlays.mate_overlay_dir(home.overlay) / "pixel.json").exists()
        assert (home.overlay / rewriter._FINGERPRINT_NAME).exists()
        assert approvals.live_incomplete is False


class TestCaseFoldedCrewmateIds:
    def _sources(self, home) -> tuple[Path, Path]:
        upper = _write(home.tmp / "src" / "Pixel.json", {"name": "Pixel", "mcpServers": {}})
        lower = home.tmp / "src" / "pixel.json"
        if lower.exists():
            pytest.skip("this filesystem folds case, so the two ids are one file")
        return upper, _write(lower, {"name": "pixel", "mcpServers": {}})

    def _publish(self, spec, target: Path) -> int:
        _write(target, spec)
        return 0

    def test_two_ids_that_are_distinct_files_both_get_an_overlay(self, home):
        upper, lower = self._sources(home)
        out = mate_overlays.rewrite_mate_overlays(
            mate_overlays.MateSources((upper, lower)),
            overlay_dir=home.overlay,
            taken_identities=set(),
            publish=self._publish,
        )
        assert out.written == {"Pixel.json", "pixel.json"} and not out.incomplete

    def test_two_ids_that_are_one_overlay_file_keep_the_first(self, home, monkeypatch):
        upper, lower = self._sources(home)
        monkeypatch.setattr(rewriter, "_overlay_names_collide", lambda d, a, b: True)
        out = mate_overlays.rewrite_mate_overlays(
            mate_overlays.MateSources((upper, lower)),
            overlay_dir=home.overlay,
            taken_identities=set(),
            publish=self._publish,
        )
        assert out.written == {"Pixel.json"}


class TestACrewmateFaultSparesTheAgentsHalf:
    def _agent_with_credentials(self, home) -> Path:
        spec = {
            "name": "kirocrew",
            "tools": ["*"],
            "mcpServers": {SERVER: {"command": sys.executable}},
        }
        spec["mcpServers"][SERVER]["env"] = {"API_TOKEN": "a"}
        _write(home.agents / "kirocrew.json", spec)
        return env_sidecar_dir(home.overlay) / env_sidecar_name("kirocrew", SERVER)

    @pytest.mark.parametrize("where", ["listing", "rewrite"])
    def test_the_agents_overlays_are_as_without_crewmates(self, home, monkeypatch, where):
        agent_sidecar = self._agent_with_credentials(home)
        mate_sidecar = _credentialed_pixel(home)
        _rewrite(home, mates=False, pooling=False)
        without = (home.overlay / "kirocrew.json").read_text()
        (home.overlay / rewriter._FINGERPRINT_NAME).unlink()
        if where == "listing":

            def _deep(_d):
                raise RecursionError("nested too deep")

            monkeypatch.setattr(mate_overlays, "list_mate_sources", _deep)
        else:
            real = rewriter.rewrite_mate_overlays

            def _explode(sources, **kw):
                real(sources, **kw)  # writes the crewmate overlay and sidecar first
                raise RecursionError("nested too deep")

            monkeypatch.setattr(rewriter, "rewrite_mate_overlays", _explode)
        results, _target_env = _rewrite(home, pooling=False)
        assert (home.overlay / "kirocrew.json").read_text() == without
        assert agent_sidecar.exists()
        assert "mates/pixel.json" not in results
        assert list(mate_overlays.mate_overlay_dir(home.overlay).glob("*.json")) == []
        assert not mate_sidecar.exists()
        assert not (home.overlay / rewriter._FINGERPRINT_NAME).exists()
        # And no launch is retired as unseen on such a pass.
        approvals = LaunchApprovals()
        _rewrite(home, pooling=False, approvals=approvals)
        assert approvals.live_incomplete is True


class TestTheSessionStartPathsThreadTheLookup:
    """``create_session`` / ``load_session`` hand the key in and the names down."""

    class _Stop(Exception):
        pass

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["create", "load"])
    async def test_the_key_reaches_the_lookup_and_the_names_reach_kas(
        self, monkeypatch, tmp_path, path
    ):
        from unittest.mock import AsyncMock

        from kiro_crew.acp import runtime_session_create, runtime_session_load
        from kiro_crew.acp.runtime import AcpRuntime

        runtime = AcpRuntime(work_dir=tmp_path, expect_mcp_reports=False, acp_backend="kas")
        runtime._initialized = True
        runtime._can_load_session = True
        seen: dict = {}

        def _pooled(overlay, agent, backend, work_dir, ref_spec=None, session_key=""):
            seen["lookup"] = (agent, backend, session_key)
            return [{"name": SERVER}], None, ()

        async def _same(agent):
            return agent

        async def _kas(agent, **kw):
            seen["kas"] = kw
            raise self._Stop

        for module in (runtime_session_create, runtime_session_load):
            monkeypatch.setattr(module, "_pooled_session_servers_and_ref_spec", _pooled)
        monkeypatch.setattr(runtime, "_source_agent", _same)
        monkeypatch.setattr(runtime, "_refuse_over_ceiling_start", AsyncMock())
        monkeypatch.setattr(runtime, "_session_work_dir", AsyncMock(return_value=tmp_path))
        monkeypatch.setattr(runtime, "_unpooled_control_planes", AsyncMock(return_value=[]))
        monkeypatch.setattr(runtime, "_own_stub_session", AsyncMock(return_value=([], "")))
        monkeypatch.setattr(runtime, "_write_remote_token", AsyncMock())
        monkeypatch.setattr(runtime, "_remote_session_servers", lambda servers: servers)
        monkeypatch.setattr(runtime, "_kas_custom_agents", _kas)
        with pytest.raises(self._Stop):
            if path == "create":
                await runtime.create_session(agent=WORKER, session_key="chat-1")
            else:
                await runtime.load_session("", "acp-1", agent=WORKER, session_key="chat-1")
        assert seen["lookup"] == (WORKER, "kas", "chat-1")
        assert seen["kas"]["session_key"] == "chat-1"
        assert seen["kas"]["worker_stub_names"] == frozenset({SERVER})
