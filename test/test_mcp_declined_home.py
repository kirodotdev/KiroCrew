"""An ``mcp.json`` server must reach new sessions on a refused shared agent home.

Shape under test: an instance on a non-default ``KIROCREW_HOME`` shares
``~/.kiro/agents`` with a default-home install. ``_decline_shared_agent_home``
refuses every spec rebuild from it, which is correct -- but the primary spec pins
``includeMcpJson: false``, so the rebuild is the ONLY way a server added to
``~/.kiro/settings/mcp.json`` reaches a session, while the gateway's probe starts
the server from the source file and shows it Online. A gateway stub reaches the
session regardless, because the overlay is injected at ``session/new`` from this
instance's own data home.

The refused rebuild projects this instance's own servers privately, and the
session path delivers them at ``session/new``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

#: An absolute command that exists on every CI OS, so command resolution keeps it.
_CMD = sys.executable


@pytest.fixture(autouse=True)
def _pin_default_home(monkeypatch, tmp_path):
    """Keep the guard's default-home comparison and breadcrumb off the real home."""
    from kiro_crew.config import paths

    monkeypatch.setattr(paths, "_resolved_home", None, raising=False)
    monkeypatch.setattr(paths, "_resolve_default_home", lambda: tmp_path / "pinned-default-home")
    monkeypatch.setattr(paths, "_write_recovery_breadcrumb", lambda data_home: None, raising=False)
    monkeypatch.delenv("KIRO_HOME", raising=False)
    monkeypatch.delenv("KIROCREW_POD", raising=False)


@pytest.fixture(autouse=True)
def _fresh_projection():
    """The projection is process memory; no test may see another's."""
    from kiro_crew import mcp_declined_home

    mcp_declined_home.clear_projection()
    yield
    mcp_declined_home.clear_projection()


def _shared_agents(monkeypatch, agents_dir: Path) -> None:
    """Make *agents_dir* both the write target and the ambient (shared) agents dir."""
    from kiro_crew import agent

    monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", agents_dir)
    monkeypatch.setattr(agent, "ambient_agents_dir", lambda: agents_dir)


def _durable_checkout(monkeypatch) -> None:
    from kiro_crew import agent

    monkeypatch.setattr(agent, "__file__", "/durable-install/KiroCrew/src/kiro_crew/agent.py")


def _write_spec(agents_dir: Path, spec: dict) -> None:
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "kirocrew.json").write_text(json.dumps(spec), encoding="utf-8")


#: What a DEFAULT-home install leaves in the shared agents dir: managed entries
#: pinned to no ``KIROCREW_HOME`` (the default-home writer's signature).
_DEFAULT_HOME_SPEC = {
    "name": "kirocrew",
    "includeMcpJson": False,
    "mcpServers": {
        "kirocrew-core": {"command": "kirocrew", "args": ["mcp", "core"], "env": {}},
        "taskei": {"command": "taskei-mcp"},
    },
    "tools": ["@kirocrew-core", "@taskei"],
}


def _declined_home(monkeypatch, tmp_path, settings_servers: dict) -> Path:
    """A non-default home beside a default-home spec, with *settings_servers*.

    Returns the shared agents dir. Nothing is faked on the decision path: the
    real guard refuses because the shared spec carries the default-home signature.
    """
    from kiro_crew import agent

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "relocated-home"))
    _durable_checkout(monkeypatch)
    shared = tmp_path / "agents"
    _write_spec(shared, _DEFAULT_HOME_SPEC)
    _shared_agents(monkeypatch, shared)
    settings = tmp_path / "settings-mcp.json"
    settings.write_text(json.dumps({"mcpServers": settings_servers}), encoding="utf-8")
    monkeypatch.setattr(agent, "_KIRO_MCP_JSON", settings)
    return shared


def _kiro_session_array(tmp_path, stubs=()) -> list[dict]:
    """What a kiro ``session/new`` receives in ``mcpServers`` (the shared append)."""
    from kiro_crew.acp.client import AcpClient

    client = AcpClient(work_dir=tmp_path / "work", agent="kirocrew")
    client._pooled_broker_stubs = lambda: [dict(s) for s in stubs]  # type: ignore[method-assign]
    return client._pooled_mcp_servers()


class TestDeclinedAgentHomeDelivery:
    def test_mcp_json_server_reaches_session_new_through_the_real_decline(
        self, monkeypatch, tmp_path
    ):
        """The reported shape, end to end: refused rebuild -> session/new array.

        Reverted (no projection on the refused rebuild, or no append on the
        session path), ``outlook`` is absent from what kiro-cli receives -- the
        Online-but-no-tools report.
        """
        from kiro_crew import agent

        shared = _declined_home(
            monkeypatch,
            tmp_path,
            {
                "outlook": {"command": _CMD, "args": ["--mcp"], "autoApprove": ["*"]},
                "taskei": {"command": _CMD},
                "muted": {"command": _CMD, "disabled": True},
                "remote": {"url": "https://example.invalid/mcp"},
            },
        )

        _path, wrote = agent.rebuild_agent_config_reporting()

        assert wrote is False, "the default-home install's shared spec must not be written"
        spec = json.loads((shared / "kirocrew.json").read_text(encoding="utf-8"))
        assert spec == _DEFAULT_HOME_SPEC, "the other install's spec must stay unchanged"

        by_name = {e["name"]: e for e in _kiro_session_array(tmp_path)}
        # taskei: already in the shared spec. muted: disabled. remote: not stdio.
        assert set(by_name) == {"outlook"}, by_name
        outlook = by_name["outlook"]
        assert os.path.normcase(outlook["command"]) == os.path.normcase(_CMD)
        assert outlook["args"] == ["--mcp"]
        # No grant rides along, and no session credential reaches a third party.
        assert "autoApprove" not in outlook
        assert all("TOKEN" not in pair["name"] for pair in outlook["env"])

    def test_a_stubbed_name_is_not_registered_twice(self, monkeypatch, tmp_path):
        from kiro_crew import agent

        _declined_home(monkeypatch, tmp_path, {"outlook": {"command": _CMD}})
        agent.rebuild_agent_config()
        stub = {"name": "outlook", "command": "/stub", "args": [], "env": [], "type": "stdio"}

        array = _kiro_session_array(tmp_path, stubs=[stub])

        assert [e["name"] for e in array] == ["outlook"]
        assert array[0]["command"] == "/stub"

    def test_nothing_is_delivered_once_the_home_owns_its_spec(self, monkeypatch, tmp_path):
        """A written spec mounts the server itself; the projection is retired."""
        from kiro_crew import agent

        _declined_home(monkeypatch, tmp_path, {"outlook": {"command": _CMD}})
        agent.rebuild_agent_config()
        assert [e["name"] for e in _kiro_session_array(tmp_path)] == ["outlook"]

        # The other install's specs are gone: this home now writes its own.
        for spec in (tmp_path / "agents").glob("kirocrew*.json"):
            spec.unlink()
        _path, wrote = agent.rebuild_agent_config_reporting()

        assert wrote is True
        assert _kiro_session_array(tmp_path) == []

    def test_no_file_in_the_data_home_decides_what_a_session_launches(self, monkeypatch, tmp_path):
        """Delivery is recomputed from the sources, never read back from disk.

        A writable cache in the data home would let its writer pick the next
        session's command; the session must see only what ``mcp.json`` says.
        """
        from kiro_crew import agent
        from kiro_crew.config.paths import config_dir

        _declined_home(monkeypatch, tmp_path, {"outlook": {"command": _CMD, "args": ["ok"]}})
        before = set(Path(config_dir()).rglob("*")) if Path(config_dir()).exists() else set()
        agent.rebuild_agent_config()
        after = set(Path(config_dir()).rglob("*")) if Path(config_dir()).exists() else set()
        assert not [p for p in after - before if "mcp" in p.name], after - before

        # A planted file with the old projection's name changes nothing.
        planted = Path(config_dir()) / "declined-home-mcp.json"
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.write_text(
            json.dumps({"servers": {"outlook": {"command": "/evil", "args": []}}}),
            encoding="utf-8",
        )
        array = _kiro_session_array(tmp_path)
        assert [(e["name"], e["args"]) for e in array] == [("outlook", ["ok"])]
        assert os.path.normcase(array[0]["command"]) == os.path.normcase(_CMD)

    def test_an_entry_past_a_bound_is_refused_whole_and_the_count_is_capped(
        self, monkeypatch, tmp_path
    ):
        from kiro_crew import agent, mcp_declined_home

        monkeypatch.setattr(mcp_declined_home, "MAX_DELIVERED_SERVERS", 2)
        limit = mcp_declined_home.MAX_FIELD_CHARS
        servers = {
            "huge-arg": {"command": _CMD, "args": ["x" * (limit + 1)]},
            "many-args": {"command": _CMD, "args": ["a"] * (mcp_declined_home.MAX_SERVER_ARGS + 1)},
            "huge-env": {"command": _CMD, "env": {"K": "v" * (limit + 1)}},
            "many-env": {
                "command": _CMD,
                "env": {f"K{i}": "v" for i in range(mcp_declined_home.MAX_SERVER_ENV + 1)},
            },
            "s1": {"command": _CMD},
            "s2": {"command": _CMD},
            "s3": {"command": _CMD},
        }
        _declined_home(monkeypatch, tmp_path, servers)
        agent.rebuild_agent_config()
        assert [e["name"] for e in _kiro_session_array(tmp_path)] == ["s1", "s2"]

    def test_the_bounds_admit_any_realistic_entry(self):
        """Generous on purpose: a long token and many args still reach the session."""
        from kiro_crew import mcp_declined_home

        assert mcp_declined_home.MAX_FIELD_CHARS >= 65536
        assert mcp_declined_home.MAX_SERVER_ARGS >= 1024
        assert mcp_declined_home.MAX_DELIVERED_SERVERS >= 256

    def test_an_entry_is_delivered_whole_with_launch_fields_only(self, monkeypatch, tmp_path):
        """No size bound (the written spec has none); only the launch fields ride."""
        from kiro_crew import agent, mcp_declined_home

        servers = {
            "long-token": {"command": _CMD, "env": {"TOKEN": "v" * 10000}},
            "many-args": {"command": _CMD, "args": ["a"] * 500},
            "s1": {"command": _CMD, "env": {"A": "1"}, "timeout": 5, "extra": {"deep": ["x"]}},
        }
        _declined_home(monkeypatch, tmp_path, servers)
        agent.rebuild_agent_config()

        array = {e["name"]: e for e in _kiro_session_array(tmp_path)}
        assert sorted(array) == ["long-token", "many-args", "s1"]
        assert array["long-token"]["env"] == [{"name": "TOKEN", "value": "v" * 10000}]
        assert len(array["many-args"]["args"]) == 500
        assert set(array["s1"]) == {"name", "command", "args", "env"}
        stored = mcp_declined_home.refresh_projection()
        assert all(set(entry) == {"command", "args", "env"} for entry in stored.values())

    def test_a_collision_suffixed_mount_is_still_delivered(self, monkeypatch, tmp_path):
        """``foo/bar`` and ``foo-bar`` share an alias; the rebuild suffixes one."""
        from kiro_crew import agent

        _declined_home(
            monkeypatch,
            tmp_path,
            {
                "foo/bar": {"command": _CMD, "args": ["slash"]},
                "foo-bar": {"command": _CMD, "args": ["dash"]},
            },
        )
        agent.rebuild_agent_config()

        array = _kiro_session_array(tmp_path)
        assert sorted(e["args"][0] for e in array) == ["dash", "slash"], array
        assert len({e["name"] for e in array}) == 2

    @pytest.mark.asyncio
    async def test_kas_gets_no_delivery(self, monkeypatch, tmp_path):
        """KAS mounts mcp.json itself, so nothing is put on its wire: a delivered
        copy would shadow the native mount with one carrying the credentials.
        Delivery is kiro-only, on both paths (KAS gets a grant instead, below).
        """
        from kiro_crew import agent
        from kiro_crew.acp.client import AcpClient
        from kiro_crew.acp.runtime import AcpRuntime
        from kiro_crew.acp_backends import ACP_BACKEND_KAS

        _declined_home(
            monkeypatch, tmp_path, {"outlook": {"command": _CMD, "env": {"API_KEY": "secret"}}}
        )
        agent.rebuild_agent_config()
        stubs = [{"name": "kirocrew-core", "command": "/stub", "args": [], "env": []}]

        fake = SimpleNamespace(acp_backend=ACP_BACKEND_KAS, _agent="kirocrew")
        assert await AcpRuntime._with_declined_home_servers(fake, stubs, None, tmp_path) == stubs

        client = AcpClient(
            work_dir=tmp_path / "work", agent="kirocrew", acp_backend=ACP_BACKEND_KAS
        )
        client._pooled_broker_stubs = lambda: []  # type: ignore[method-assign]
        assert client._kiro_session_servers() == []
        # The same sources do deliver on kiro, so the gate is what withheld it.
        assert [e["name"] for e in _kiro_session_array(tmp_path)] == ["outlook"]

    def test_a_server_with_disabled_tools_is_not_delivered(self, monkeypatch, tmp_path, caplog):
        """An ACP element mounts every tool, so a per-tool restriction cannot ride it."""
        from kiro_crew import agent

        _declined_home(
            monkeypatch,
            tmp_path,
            {
                "outlook": {"command": _CMD, "disabledTools": ["send_mail"]},
                "calendar": {"command": _CMD, "disabledTools": []},
            },
        )
        with caplog.at_level("WARNING", logger="kiro_crew.mcp_declined_home"):
            agent.rebuild_agent_config()
            names = [e["name"] for e in _kiro_session_array(tmp_path)]

        assert names == ["calendar"], names
        assert any(
            "disabledTools" in r.getMessage() and "outlook" in r.getMessage()
            for r in caplog.records
        )

    def test_the_projection_writes_no_grant_audit_record(self, monkeypatch, tmp_path):
        """The in-memory pass never writes a spec, so it must not log grants as if it had."""
        from kiro_crew import agent, mcp_declined_home

        _declined_home(monkeypatch, tmp_path, {"outlook": {"command": _CMD}})
        ops: list[str] = []

        class _Sel:
            def log_api_access(self, **kw):
                ops.append(kw.get("operation"))

        monkeypatch.setattr(agent, "sel", lambda: _Sel())
        assert sorted(mcp_declined_home.refresh_projection()) == ["outlook"]
        assert "mcp_tools_added" not in ops, ops

    def test_the_projection_runs_the_app_pass_unaudited(self, monkeypatch, tmp_path):
        """App servers are collected too; that pass must record nothing either."""
        from kiro_crew import mcp_declined_home
        from kiro_crew.agent_materialization import mcp_sources

        _declined_home(monkeypatch, tmp_path, {"outlook": {"command": _CMD}})
        seen: list[bool] = []

        def _apps(*, audit=True):
            seen.append(audit)
            return {}

        monkeypatch.setattr(mcp_sources, "_collect_app_mcp_servers", _apps)
        assert sorted(mcp_declined_home.refresh_projection()) == ["outlook"]
        assert seen == [False], seen

    def test_an_unresolved_command_is_re_resolved_on_the_next_start(self, monkeypatch, tmp_path):
        """A pass that left a command unresolved is not memoized."""
        from kiro_crew import mcp_declined_home

        _declined_home(
            monkeypatch,
            tmp_path,
            {"outlook": {"command": _CMD}, "later": {"command": "not-installed-yet-xyz"}},
        )
        calls = 0
        real = mcp_declined_home._compute_projection

        def counting():
            nonlocal calls
            calls += 1
            return real()

        monkeypatch.setattr(mcp_declined_home, "_compute_projection", counting)
        mcp_declined_home.refresh_projection()
        mcp_declined_home.refresh_projection()
        assert calls == 2

        # With everything resolved, an unchanged source is not recomputed.
        _declined_home(monkeypatch, tmp_path, {"outlook": {"command": _CMD}})
        calls = 0
        mcp_declined_home.refresh_projection()
        mcp_declined_home.refresh_projection()
        assert calls == 1

    @pytest.mark.skipif(os.name == "nt", reason="POSIX executable bits")
    def test_a_removed_binary_is_re_resolved_not_launched_from_the_memo(
        self, monkeypatch, tmp_path
    ):
        from kiro_crew import mcp_declined_home

        first, second = tmp_path / "bin1", tmp_path / "bin2"
        for d in (first, second):
            d.mkdir()
            exe = d / "probe-mcp-xyz"
            exe.write_text("#!/bin/sh\n", encoding="utf-8")
            exe.chmod(0o755)
        _declined_home(monkeypatch, tmp_path, {"outlook": {"command": "probe-mcp-xyz"}})
        monkeypatch.setenv("PATH", f"{first}{os.pathsep}{second}")
        assert mcp_declined_home.refresh_projection()["outlook"]["command"] == str(
            first / "probe-mcp-xyz"
        )
        (first / "probe-mcp-xyz").unlink()
        assert mcp_declined_home.refresh_projection()["outlook"]["command"] == str(
            second / "probe-mcp-xyz"
        )

    @pytest.mark.asyncio
    async def test_the_entitlement_probe_starts_no_delivered_server(self):
        """The probe takes the broker stubs alone, not the declined-home append."""
        import inspect

        from kiro_crew.acp.client import AcpClient

        src = inspect.getsource(AcpClient)
        probe = src[src.index("require_unchanged_derived_spec, self._derived_spec_snapshot") :]
        probe = probe[: probe.index("METHOD_SESSION_NEW")]
        assert "self._pooled_broker_stubs" in probe
        assert "self._pooled_mcp_servers" not in probe

    def test_delivery_is_not_filtered_by_the_shared_allowed_tools(self, monkeypatch, tmp_path):
        """Parity with the written spec, which mounts the server under the same
        ``allowedTools``: a grant there applies here exactly as it would there."""
        from kiro_crew import agent

        servers = {"outlook": {"command": _CMD}, "calendar": {"command": _CMD}}
        shared = _declined_home(monkeypatch, tmp_path, servers)
        _write_spec(shared, dict(_DEFAULT_HOME_SPEC, allowedTools=["grep", "@outlook", "*"]))
        agent.rebuild_agent_config()
        assert [e["name"] for e in _kiro_session_array(tmp_path)] == ["calendar", "outlook"]

    def test_a_host_that_composes_its_own_array_gains_nothing(self, monkeypatch, tmp_path):
        """Delivery is for hosts that mount spec servers off the wire, by name."""
        from kiro_crew import agent
        from kiro_crew.acp.client import AcpClient
        from kiro_crew.acp_backends import ACP_BACKEND_DEEPSEEK, ACP_BACKENDS_SPEC_SERVERS_OFF_WIRE

        assert ACP_BACKEND_DEEPSEEK not in ACP_BACKENDS_SPEC_SERVERS_OFF_WIRE
        _declined_home(monkeypatch, tmp_path, {"outlook": {"command": _CMD}})
        agent.rebuild_agent_config()

        client = AcpClient(
            work_dir=tmp_path / "work", agent="kirocrew", acp_backend=ACP_BACKEND_DEEPSEEK
        )
        client._pooled_broker_stubs = lambda: []  # type: ignore[method-assign]
        assert client._kiro_session_servers() == []
        assert [e["name"] for e in _kiro_session_array(tmp_path)] == ["outlook"]

    def test_skipped_server_diagnostics_keep_a_bounded_sample(self, monkeypatch, tmp_path, caplog):
        from kiro_crew import agent, mcp_declined_home

        many = mcp_declined_home.MAX_LOGGED_NAMES + 5
        servers = {f"r{i:02d}": {"url": f"https://example.invalid/{i}"} for i in range(many)}
        _declined_home(monkeypatch, tmp_path, servers)
        with caplog.at_level("WARNING", logger="kiro_crew.mcp_declined_home"):
            agent.rebuild_agent_config()

        lines = [r.getMessage() for r in caplog.records if "remote MCP" in r.getMessage()]
        assert len(lines) == 1, lines
        assert f"{many} remote MCP" in lines[0]
        cap = mcp_declined_home.MAX_LOGGED_NAMES
        assert all(f"r{i:02d}" in lines[0] for i in range(cap))
        assert f"r{cap:02d}" not in lines[0]
        assert "(+5 more)" in lines[0]

    def test_the_projection_mounts_what_a_written_spec_would(self, monkeypatch, tmp_path):
        """Both channels apply one set of predicates; pin them equal.

        The same sources are rebuilt once on a home that OWNS its spec (written
        channel) and projected once (session channel). Every user-installed
        stdio server the written spec mounts must be projected with the same
        launch fields, and nothing else may be.
        """
        from kiro_crew import agent, mcp_declined_home

        sources = {
            "outlook": {"command": _CMD, "args": ["--mcp"], "env": {"A": "1"}},
            "foo/bar": {"command": _CMD, "args": ["slash"]},
            "foo-bar": {"command": _CMD, "args": ["dash"]},
            "muted": {"command": _CMD, "disabled": True},
            "remote": {"url": "https://example.invalid/mcp"},
        }
        shared = _declined_home(monkeypatch, tmp_path, sources)
        for spec in shared.glob("kirocrew*.json"):
            spec.unlink()
        _path, wrote = agent.rebuild_agent_config_reporting()
        assert wrote is True
        written = json.loads((shared / "kirocrew.json").read_text(encoding="utf-8"))
        refs = set(written.get("tools") or [])
        # Every source above launches _CMD; Crew's own managed servers do not.
        written_mounts = {
            name: {
                "command": entry["command"],
                "args": list(entry.get("args") or []),
                "env": dict(entry.get("env") or {}),
            }
            for name, entry in written["mcpServers"].items()
            if entry.get("command") == _CMD
            and not entry.get("disabled")
            and ("*" in refs or f"@{name}" in refs)
        }
        assert len(written_mounts) == 3, written_mounts  # outlook + both foo servers

        projected = mcp_declined_home.refresh_projection()

        assert set(projected) == set(written_mounts), (projected, written_mounts)
        assert projected == written_mounts

    def test_registry_mode_delivers_nothing(self, monkeypatch, tmp_path):
        from kiro_crew import agent

        _declined_home(monkeypatch, tmp_path, {"outlook": {"command": _CMD}})
        agent.rebuild_agent_config()
        monkeypatch.setattr(agent, "_mcp_registry_mode", lambda: True)

        assert _kiro_session_array(tmp_path) == []

    def test_an_unchanged_source_is_not_recomputed_and_an_edit_is(self, monkeypatch, tmp_path):
        """The refresh poll re-runs the refused rebuild; it must not redo the passes."""
        from kiro_crew import agent, mcp_declined_home

        _declined_home(monkeypatch, tmp_path, {"outlook": {"command": _CMD}})
        calls: list[int] = []
        real = mcp_declined_home._compute_projection

        def counting():
            calls.append(1)
            return real()

        monkeypatch.setattr(mcp_declined_home, "_compute_projection", counting)
        agent.rebuild_agent_config()
        agent.rebuild_agent_config()
        assert len(calls) == 1

        settings = tmp_path / "settings-mcp.json"
        settings.write_text(
            json.dumps({"mcpServers": {"calendar": {"command": _CMD, "args": ["x"]}}}),
            encoding="utf-8",
        )
        agent.rebuild_agent_config()
        assert len(calls) == 2
        assert [e["name"] for e in _kiro_session_array(tmp_path)] == ["calendar"]

    def test_another_agent_gets_nothing(self, monkeypatch, tmp_path):
        from kiro_crew import agent, mcp_declined_home

        _declined_home(monkeypatch, tmp_path, {"outlook": {"command": _CMD}})
        agent.rebuild_agent_config()

        assert mcp_declined_home.session_servers("kirocrew-worker") == []

    @pytest.mark.asyncio
    async def test_the_shared_runtime_appends_after_the_stubs(self, monkeypatch, tmp_path):
        """AcpRuntime on kiro takes the same delivery; other hosts do not."""
        from kiro_crew import agent
        from kiro_crew.acp.runtime import AcpRuntime
        from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_KIRO

        _declined_home(monkeypatch, tmp_path, {"outlook": {"command": _CMD}})
        agent.rebuild_agent_config()
        stubs = [{"name": "kirocrew-core", "command": "/stub", "args": [], "env": []}]

        fake = SimpleNamespace(acp_backend=ACP_BACKEND_KIRO, _agent="kirocrew")
        out = await AcpRuntime._with_declined_home_servers(fake, stubs, None, tmp_path)
        assert [e["name"] for e in out] == ["kirocrew-core", "outlook"]

        fake.acp_backend = ACP_BACKEND_CLAUDE
        assert await AcpRuntime._with_declined_home_servers(fake, stubs, None, tmp_path) == stubs


class TestWrittenSpecParity:
    def test_a_declared_but_unreferenced_spec_server_is_not_treated_as_mounted(
        self, monkeypatch, tmp_path
    ):
        """The rebuild leaves a muted server declared with no ``tools`` ref, and
        kiro-cli never mounts that shape; once this instance's mcp.json enables
        it, delivery and the KAS grant must still supply it."""
        from kiro_crew import agent, mcp_declined_home

        shared = _declined_home(monkeypatch, tmp_path, {"outlook": {"command": _CMD}})
        stale = dict(_DEFAULT_HOME_SPEC)
        stale["mcpServers"] = dict(
            stale["mcpServers"], outlook={"command": "old", "disabled": True}
        )
        _write_spec(shared, stale)
        agent.rebuild_agent_config()
        assert [e["name"] for e in _kiro_session_array(tmp_path)] == ["outlook"]
        assert mcp_declined_home.kas_tool_grants(
            "kirocrew", spec=stale, work_dir=tmp_path / "work"
        ) == ["@outlook"]
        # Referenced (or covered by "*"), it is mounted and skipped as before.
        assert mcp_declined_home._declared_names(dict(stale, tools=["@outlook"])) >= {"outlook"}
        assert "outlook" in mcp_declined_home._declared_names(dict(stale, tools=["*"]))

    def test_a_written_spec_mounts_and_pre_approves_the_same_server(self, monkeypatch, tmp_path):
        """The bar both channels meet. A home that owns its spec writes the
        mcp.json server into ``mcpServers`` and ``tools`` and also grants it in
        ``allowedTools``; session delivery mounts the same entry and grants nothing."""
        from kiro_crew import agent
        from kiro_crew.agent_files import AGENT_FILENAME

        _declined_home(monkeypatch, tmp_path, {"outlook": {"command": _CMD}})
        agents = tmp_path / "agents"
        for stale in agents.glob("kirocrew*.json"):
            stale.unlink()
        _path, wrote = agent.rebuild_agent_config_reporting()
        assert wrote is True
        spec = json.loads((agents / AGENT_FILENAME).read_text(encoding="utf-8"))
        assert "outlook" in spec["mcpServers"]
        assert "@outlook" in spec["tools"]
        # The written spec pre-approves the whole server. Delivery adds no approval
        # of its own, so it is never more permissive than this.
        assert "@outlook" in spec["allowedTools"]


class TestWorkspaceSettingsRead:
    def test_a_link_to_a_credential_file_is_not_read(self, monkeypatch, tmp_path):
        """The checkout is untrusted; a link it plants must not make the gateway
        read a credential file. Refused reads answer ``{"*"}`` (grant nothing)."""
        from kiro_crew import mcp_declined_home

        home = tmp_path / "home"
        (home / ".docker").mkdir(parents=True)
        target = home / ".docker" / "config.json"
        target.write_text(json.dumps({"mcpServers": {"x": {"command": "a"}}}), encoding="utf-8")
        ws = tmp_path / "work" / ".kiro" / "settings"
        ws.mkdir(parents=True)
        try:
            (ws / "mcp.json").symlink_to(target)
        except OSError:
            pytest.skip("symlinks unavailable")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        assert mcp_declined_home._workspace_names(tmp_path / "work") == {"*"}

    def test_a_plain_file_and_an_absent_one_still_read(self, tmp_path):
        from kiro_crew import mcp_declined_home

        work = tmp_path / "work"
        assert mcp_declined_home._workspace_names(work) == set()
        ws = work / ".kiro" / "settings"
        ws.mkdir(parents=True)
        (ws / "mcp.json").write_text(
            json.dumps({"mcpServers": {"foo/bar": {"command": "a"}}}), encoding="utf-8"
        )
        assert mcp_declined_home._workspace_names(work) == {"foo/bar", "foo-bar"}


class TestSupplyAudit:
    def test_a_change_in_what_sessions_are_supplied_writes_one_sel_record(
        self, monkeypatch, tmp_path
    ):
        """Parity with the written spec's ``mcp_tools_added``: the tool surface
        change is recorded once per change, not once per session."""
        from kiro_crew import agent, mcp_declined_home

        _declined_home(monkeypatch, tmp_path, {"outlook": {"command": _CMD}})
        records: list[dict] = []

        class _Sel:
            def log_api_access(self, **kw):
                records.append(kw)

        monkeypatch.setattr(agent, "sel", lambda: _Sel())
        mcp_declined_home.refresh_projection()
        mcp_declined_home.refresh_projection()
        supplied = [r for r in records if r.get("operation") == "mcp_declined_home_supplied"]
        assert len(supplied) == 1, records
        assert "outlook" in supplied[0]["resources"]

    def test_a_suffixed_other_scope_entry_does_not_withhold_the_global_grant(
        self, monkeypatch, tmp_path
    ):
        """``foo/bar`` in another scope collides with global ``foo-bar`` and is
        mounted as ``foo-bar-2``; it overrides nothing, so KAS still gets ``@foo-bar``."""
        from kiro_crew import mcp_declined_home

        class _Sources:
            kiro_global = {"foo-bar": {"command": _CMD}}
            scopes = [("kiro-global", kiro_global), ("store", {"foo/bar": {"command": _CMD}})]

        native = mcp_declined_home._kas_native_names(
            _Sources(), {"foo-bar": "foo-bar", "foo/bar": "foo-bar-2"}, {"foo-bar": {}}
        )
        assert native == frozenset({"foo-bar"})


async def _kas_custom_agent(monkeypatch, shared: Path, work_dir: Path, stubbed=frozenset()):
    """The ``customAgents`` entry a KAS ``session/new`` carries, built by the harness."""
    from kiro_crew import agent as agent_mod
    from kiro_crew.acp.harness.kas import KasHarness
    from kiro_crew.config import paths as paths_mod
    from kiro_crew.mcp_gateway import session_servers as session_servers_mod

    monkeypatch.setattr(agent_mod, "require_fork_governance", lambda agent, work_dir: None)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda agent: None)
    monkeypatch.setattr(
        session_servers_mod,
        "injection_server_names",
        lambda overlay, agent, **kw: frozenset(stubbed),
    )
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: shared)
    work_dir.mkdir(parents=True, exist_ok=True)
    extras = await KasHarness().session_extras("kirocrew", work_dir=str(work_dir))
    assert extras.custom_agents and len(extras.custom_agents) == 1
    return extras.custom_agents[0]


class TestKasRefusedHomeGrant:
    """KAS mounts ``~/.kiro/settings/mcp.json`` on its own and grants only what the
    agent's ``tools`` names (measured on ``kiro-cli acp --agent-engine v3``: an
    ungranted server connects and lists its tools, and the model cannot call
    them). So the refused home's fix on KAS is the ``@name`` grant alone.
    """

    @pytest.mark.asyncio
    async def test_the_server_is_granted_and_nothing_crosses_the_wire(self, monkeypatch, tmp_path):
        """Reverted (no grant in the harness), ``@outlook`` is absent from ``tools``
        and KAS keeps the server mounted but uncallable -- the reported shape."""
        from kiro_crew import agent
        from kiro_crew.acp.runtime import AcpRuntime
        from kiro_crew.acp_backends import ACP_BACKEND_KAS

        shared = _declined_home(
            monkeypatch,
            tmp_path,
            {"outlook": {"command": _CMD, "env": {"API_KEY": "s3cr3t-value"}}},
        )
        agent.rebuild_agent_config()

        custom = await _kas_custom_agent(monkeypatch, shared, tmp_path / "work")

        assert custom["tools"].count("@outlook") == 1, custom["tools"]
        assert custom["tools"].count("@taskei") == 1
        assert "outlook" not in (custom.get("mcpServers") or {})
        assert "s3cr3t-value" not in json.dumps(custom)
        # Visibility, not approval: no permission entry names the granted server.
        assert "outlook" not in json.dumps(custom.get("permissions") or {})
        # And the session-level array carries none of it.
        fake = SimpleNamespace(acp_backend=ACP_BACKEND_KAS, _agent="kirocrew")
        assert await AcpRuntime._with_declined_home_servers(fake, [], None, tmp_path) == []

    @pytest.mark.asyncio
    async def test_a_writable_home_projects_unchanged(self, monkeypatch, tmp_path):
        """Nothing is granted while this instance owns its spec."""
        from kiro_crew import agent

        shared = _declined_home(monkeypatch, tmp_path, {"outlook": {"command": _CMD}})
        monkeypatch.setattr(agent, "_decline_shared_agent_home", lambda audit=True: None)

        custom = await _kas_custom_agent(monkeypatch, shared, tmp_path / "work")
        assert "@outlook" not in custom["tools"]

    @pytest.mark.asyncio
    async def test_a_stubbed_name_is_not_granted_again(self, monkeypatch, tmp_path):
        from kiro_crew import agent

        shared = _declined_home(monkeypatch, tmp_path, {"outlook": {"command": _CMD}})
        agent.rebuild_agent_config()
        custom = await _kas_custom_agent(
            monkeypatch, shared, tmp_path / "work", stubbed={"outlook"}
        )
        assert "@outlook" not in custom["tools"]

    def test_the_grant_reads_the_spec_the_permissions_were_built_from(self, monkeypatch, tmp_path):
        """One observation: a server the loaded spec mounts is not granted again,
        even after the file stops declaring it."""
        from kiro_crew import agent, mcp_declined_home

        shared = _declined_home(monkeypatch, tmp_path, {"outlook": {"command": _CMD}})
        agent.rebuild_agent_config()
        _write_spec(shared, dict(_DEFAULT_HOME_SPEC))
        mounted = dict(_DEFAULT_HOME_SPEC)
        mounted["mcpServers"] = dict(mounted["mcpServers"], outlook={"command": _CMD})
        mounted["tools"] = [*mounted["tools"], "@outlook"]
        grant = mcp_declined_home.kas_tool_grants
        assert grant("kirocrew", spec=mounted, work_dir=tmp_path / "work") == []
        assert grant("kirocrew", spec=dict(_DEFAULT_HOME_SPEC), work_dir=tmp_path / "work") == [
            "@outlook"
        ]

    def _grants(self, tmp_path, tools=None, present=(), work_dir=None):
        """Grants for the shared spec as the harness would have loaded it."""
        from kiro_crew import agent as agent_mod
        from kiro_crew import mcp_declined_home
        from kiro_crew.agent_files import AGENT_FILENAME

        spec = json.loads(
            (agent_mod.kiro_agents_dir_path() / AGENT_FILENAME).read_text(encoding="utf-8")
        )
        if tools is not None:
            spec["tools"] = tools
        return mcp_declined_home.kas_tool_grants(
            "kirocrew",
            spec=spec,
            work_dir=work_dir or tmp_path / "work",
            present=present,
        )

    def test_muted_and_registry_governed_servers_are_not_granted(self, monkeypatch, tmp_path):
        from kiro_crew import agent

        _declined_home(
            monkeypatch,
            tmp_path,
            {
                "outlook": {"command": _CMD},
                "muted": {"command": _CMD, "disabled": True},
                "catalog": {"command": _CMD, "type": "registry"},
            },
        )
        agent.rebuild_agent_config()
        assert self._grants(tmp_path) == ["@outlook"]

    def test_registry_mode_grants_nothing(self, monkeypatch, tmp_path):
        from kiro_crew import agent

        _declined_home(monkeypatch, tmp_path, {"outlook": {"command": _CMD}})
        agent.rebuild_agent_config()
        assert self._grants(tmp_path) == ["@outlook"]
        monkeypatch.setattr(agent, "_mcp_registry_mode", lambda: True)
        assert self._grants(tmp_path) == []

    def test_the_grant_is_not_filtered_by_the_shared_allowed_tools(self, monkeypatch, tmp_path):
        """Parity: a written spec would carry ``@outlook`` in ``tools`` under the
        same ``allowedTools``."""
        from kiro_crew import agent

        shared = _declined_home(
            monkeypatch, tmp_path, {"outlook": {"command": _CMD}, "calendar": {"command": _CMD}}
        )
        _write_spec(shared, dict(_DEFAULT_HOME_SPEC, allowedTools=["@out*", "grep"]))
        agent.rebuild_agent_config()
        assert self._grants(tmp_path) == ["@calendar", "@outlook"]

    def test_an_existing_ref_or_star_is_not_added_again(self, monkeypatch, tmp_path):
        from kiro_crew import agent

        _declined_home(monkeypatch, tmp_path, {"outlook": {"command": _CMD}})
        agent.rebuild_agent_config()
        assert self._grants(tmp_path, tools=["fs_read", "@outlook"]) == []
        assert self._grants(tmp_path, tools=["*"]) == []
        assert self._grants(tmp_path, tools="*") == []
        # The same, absent the ref, does grant: the skip is what withheld it.
        assert self._grants(tmp_path, tools=["fs_read"]) == ["@outlook"]

    def test_a_present_stub_name_is_not_granted(self, monkeypatch, tmp_path):
        from kiro_crew import agent

        _declined_home(monkeypatch, tmp_path, {"outlook": {"command": _CMD}})
        agent.rebuild_agent_config()
        assert self._grants(tmp_path, present={"outlook"}) == []

    def test_a_name_the_workspace_mcp_json_declares_is_withheld(self, monkeypatch, tmp_path):
        """KAS mounts the checkout's mcp.json too, so the name may be a different server."""
        from kiro_crew import agent

        _declined_home(
            monkeypatch, tmp_path, {"outlook": {"command": _CMD}, "calendar": {"command": _CMD}}
        )
        agent.rebuild_agent_config()
        ws = tmp_path / "work" / ".kiro" / "settings"
        ws.mkdir(parents=True)
        (ws / "mcp.json").write_text(
            json.dumps({"mcpServers": {"outlook": {"command": "other"}}}), encoding="utf-8"
        )
        assert self._grants(tmp_path) == ["@calendar"]
        (ws / "mcp.json").write_text("{not json", encoding="utf-8")
        assert self._grants(tmp_path) == []

    def test_only_a_server_kas_mounts_itself_is_granted(self, monkeypatch, tmp_path):
        """KAS reads ``~/.kiro/settings/mcp.json`` alone, so a server only this
        instance's own store declares -- or one that store overrides -- is not
        what KAS would mount under that name."""
        from kiro_crew import agent

        _declined_home(
            monkeypatch, tmp_path, {"outlook": {"command": _CMD}, "shadowed": {"command": _CMD}}
        )
        own = agent._user_dir() / "mcp.json"
        own.parent.mkdir(parents=True, exist_ok=True)
        own.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "crewonly": {"command": _CMD},
                        "shadowed": {"command": _CMD, "args": ["--own"]},
                    }
                }
            ),
            encoding="utf-8",
        )
        agent.rebuild_agent_config()
        from kiro_crew import mcp_declined_home

        assert {"crewonly", "shadowed", "outlook"} <= set(mcp_declined_home.refresh_projection())
        assert self._grants(tmp_path) == ["@outlook"]

    def test_another_agent_gets_nothing(self, monkeypatch, tmp_path):
        from kiro_crew import agent, mcp_declined_home

        _declined_home(monkeypatch, tmp_path, {"outlook": {"command": _CMD}})
        agent.rebuild_agent_config()
        assert mcp_declined_home.kas_tool_grants("reviewer", spec={"tools": []}) == []

    def test_the_projection_adds_a_ref_at_most_once(self):
        from kiro_crew.acp.kas_agents import to_client_custom_agent

        out = to_client_custom_agent(
            "a",
            {"name": "a", "tools": ["fs_read", "@outlook"]},
            "p",
            extra_tool_refs=["@outlook", "@calendar"],
        )
        assert out["tools"] == ["fs_read", "@outlook", "@calendar"]
        assert (
            to_client_custom_agent(
                "a", {"name": "a", "tools": "*"}, "p", extra_tool_refs=["@outlook"]
            )["tools"]
            == "*"
        )
