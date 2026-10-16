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
def _pin_default_home(_floor_monkeypatch, tmp_path):
    """Keep the guard's default-home comparison and breadcrumb off the real home."""
    from kiro_crew.config import paths

    mp = _floor_monkeypatch
    mp.setattr(paths, "_resolved_home", None, raising=False)
    mp.setattr(paths, "_resolve_default_home", lambda: tmp_path / "pinned-default-home")
    mp.setattr(paths, "_write_recovery_breadcrumb", lambda data_home: None, raising=False)
    mp.delenv("KIRO_HOME", raising=False)
    mp.delenv("KIROCREW_POD", raising=False)


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
        assert await AcpRuntime._with_declined_home_servers(fake, stubs, None, tmp_path) == (
            stubs,
            None,
        )

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

        src = inspect.getsource(AcpClient._entitlement_probe_answer)
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
        out, _decision = await AcpRuntime._with_declined_home_servers(fake, stubs, None, tmp_path)
        assert [e["name"] for e in out] == ["kirocrew-core", "outlook"]

        fake.acp_backend = ACP_BACKEND_CLAUDE
        assert await AcpRuntime._with_declined_home_servers(fake, stubs, None, tmp_path) == (
            stubs,
            None,
        )


class TestInheritedApprovalCeiling:
    """The shared spec's ``allowedTools`` never passed this instance's ceiling.

    A server delivered to a session whose child loaded such a grant would run
    its tools without reaching the PreToolUse gate, so a grant covering the
    server that this instance's ceiling would not keep withholds delivery.
    """

    def _arrange(self, monkeypatch, tmp_path, allowed: list[str], *, permits: bool) -> list[str]:
        """Deliver ``outlook`` beside a shared spec carrying *allowed*; return refs asked."""
        from kiro_crew import agent, mcp_declined_home

        shared = _declined_home(monkeypatch, tmp_path, {"outlook": {"command": _CMD}})
        _write_spec(shared, dict(_DEFAULT_HOME_SPEC, allowedTools=allowed))
        asked: list[str] = []

        def ceiling(ref: str) -> bool:
            asked.append(ref)
            return permits

        monkeypatch.setattr(mcp_declined_home, "may_skip_gate_now", ceiling)
        agent.rebuild_agent_config()
        return asked

    @pytest.mark.parametrize(
        ("allowed", "ref"),
        [
            (["*"], "@outlook"),
            (["@outlook"], "@outlook"),
            (["@outlook/send_mail"], "@outlook/send_mail"),
        ],
        ids=["wildcard", "server", "server-tool"],
    )
    def test_a_grant_the_ceiling_would_not_keep_withholds_the_server(
        self, monkeypatch, tmp_path, allowed, ref
    ):
        asked = self._arrange(monkeypatch, tmp_path, allowed, permits=False)
        assert _kiro_session_array(tmp_path) == []
        assert ref in asked

    def test_a_grant_the_ceiling_keeps_still_delivers(self, monkeypatch, tmp_path):
        asked = self._arrange(monkeypatch, tmp_path, ["@outlook", "fs_read"], permits=True)
        assert [e["name"] for e in _kiro_session_array(tmp_path)] == ["outlook"]
        assert "@outlook" in asked

    def test_a_grant_for_another_server_is_not_asked(self, monkeypatch, tmp_path):
        asked = self._arrange(monkeypatch, tmp_path, ["@calendar", "fs_read"], permits=False)
        assert [e["name"] for e in _kiro_session_array(tmp_path)] == ["outlook"]
        assert not [r for r in asked if r.startswith("@outlook")]

    @pytest.mark.parametrize(
        "allowed",
        [["@*"], ["@out*"], ["@outlook/*_delete"], ["*_delete"]],
        ids=["any-server", "server-prefix", "server-tool-glob", "bare-tool-glob"],
    )
    def test_a_glob_grant_covering_the_server_withholds_it(self, monkeypatch, tmp_path, allowed):
        """kiro-cli expands allowedTools as globs, so a glob that can match the
        server's tools is a grant on it."""
        asked = self._arrange(monkeypatch, tmp_path, allowed, permits=False)
        assert _kiro_session_array(tmp_path) == []
        assert "@outlook" in asked

    @pytest.mark.parametrize(
        "allowed",
        [["@cal*"], ["@calendar/*_delete"], ["fs_*"]],
        ids=["other-server-prefix", "other-server-tool-glob", "literal-first-char"],
    )
    def test_a_glob_that_cannot_match_the_server_does_not_withhold_it(
        self, monkeypatch, tmp_path, allowed
    ):
        asked = self._arrange(monkeypatch, tmp_path, allowed, permits=False)
        assert [e["name"] for e in _kiro_session_array(tmp_path)] == ["outlook"]
        assert "@outlook" not in asked

    def _sel_records(self, monkeypatch) -> list[dict]:
        from kiro_crew import agent

        records: list[dict] = []

        class _Sel:
            def log_api_access(self, **kw):
                records.append(kw)

        monkeypatch.setattr(agent, "sel", lambda: _Sel())
        return records

    def test_a_withheld_server_writes_one_denied_record_and_is_not_supplied(
        self, monkeypatch, tmp_path
    ):
        from kiro_crew import mcp_declined_home

        records = self._sel_records(monkeypatch)
        self._arrange(monkeypatch, tmp_path, ["@outlook"], permits=False)
        _kiro_session_array(tmp_path)
        _kiro_session_array(tmp_path)
        mcp_declined_home.refresh_projection()
        withheld = [r for r in records if r.get("operation") == "mcp_declined_home_withheld"]
        assert len(withheld) == 1, records
        assert withheld[0]["outcome"] == "denied"
        assert "outlook" in withheld[0]["resources"]
        assert "governance ceiling" in withheld[0]["error"]
        supplied = [r for r in records if r.get("operation") == "mcp_declined_home_supplied"]
        assert not any(
            "outlook" in r["resources"].split(" supplied")[0] for r in supplied
        ), supplied

    def test_a_permitted_server_is_supplied_and_writes_no_denied_record(
        self, monkeypatch, tmp_path
    ):
        records = self._sel_records(monkeypatch)
        self._arrange(monkeypatch, tmp_path, ["@outlook"], permits=True)
        _kiro_session_array(tmp_path)
        assert not [r for r in records if r.get("operation") == "mcp_declined_home_withheld"]
        supplied = [r for r in records if r.get("operation") == "mcp_declined_home_supplied"]
        assert len(supplied) == 1 and "outlook" in supplied[0]["resources"], records


class TestDeliveryReadsTheLoadedSpec:
    """A kiro session with a prepared native view runs that frozen copy, so the
    ceiling and the mounted names are judged on it, not on a later shared file."""

    def _frozen_then_relaxed(self, monkeypatch, tmp_path) -> dict:
        """Freeze a spec whose ``*`` the ceiling refuses, then drop ``*`` on disk."""
        from kiro_crew import agent, mcp_declined_home

        shared = _declined_home(monkeypatch, tmp_path, {"outlook": {"command": _CMD}})
        frozen = dict(_DEFAULT_HOME_SPEC, allowedTools=["*"])
        _write_spec(shared, frozen)
        monkeypatch.setattr(mcp_declined_home, "may_skip_gate_now", lambda _ref: False)
        agent.rebuild_agent_config()
        _write_spec(shared, dict(_DEFAULT_HOME_SPEC, allowedTools=[]))
        return json.loads(json.dumps(frozen))

    def test_the_client_checks_the_view_the_session_loads(self, monkeypatch, tmp_path):
        from kiro_crew.acp.client import AcpClient

        frozen = self._frozen_then_relaxed(monkeypatch, tmp_path)
        # The live file alone would deliver: nothing there pre-approves outlook.
        assert [e["name"] for e in _kiro_session_array(tmp_path)] == ["outlook"]
        client = AcpClient(work_dir=tmp_path / "work", agent="kirocrew")
        client._pooled_broker_stubs = list  # type: ignore[method-assign]
        client._native_skill_projection = SimpleNamespace(specs={"kirocrew": frozen})
        assert client._pooled_mcp_servers() == []

    @pytest.mark.asyncio
    async def test_the_shared_runtime_checks_the_view_the_session_loads(
        self, monkeypatch, tmp_path
    ):
        from kiro_crew.acp.runtime import AcpRuntime
        from kiro_crew.acp_backends import ACP_BACKEND_KIRO

        frozen = self._frozen_then_relaxed(monkeypatch, tmp_path)
        fake = SimpleNamespace(
            acp_backend=ACP_BACKEND_KIRO,
            _agent="kirocrew",
            _native_skill_projection=SimpleNamespace(specs={"kirocrew": frozen}),
        )
        delivered, _decision = await AcpRuntime._with_declined_home_servers(
            fake, [], None, tmp_path
        )
        assert delivered == []
        fake._native_skill_projection = None
        out, _decision = await AcpRuntime._with_declined_home_servers(fake, [], None, tmp_path)
        assert [e["name"] for e in out] == ["outlook"]

    def test_a_projection_without_a_view_delivers_nothing(self, monkeypatch, tmp_path):
        from kiro_crew import agent, mcp_declined_home

        _declined_home(monkeypatch, tmp_path, {"outlook": {"command": _CMD}})
        agent.rebuild_agent_config()
        spec = mcp_declined_home.session_spec(SimpleNamespace(specs={}), "kirocrew")
        assert spec is None
        assert mcp_declined_home.session_servers("kirocrew", spec=spec) == []

    def _sel(self, monkeypatch) -> list[dict]:
        from kiro_crew import agent

        records: list[dict] = []

        class _Sel:
            def log_api_access(self, **kw):
                records.append(kw)

        monkeypatch.setattr(agent, "sel", lambda: _Sel())
        return records

    def _frozen_session(self, tmp_path, frozen: dict) -> list[dict]:
        from kiro_crew.acp.client import AcpClient

        client = AcpClient(work_dir=tmp_path / "work", agent="kirocrew")
        client._pooled_broker_stubs = list  # type: ignore[method-assign]
        client._native_skill_projection = SimpleNamespace(specs={"kirocrew": frozen})
        return client._pooled_mcp_servers()

    def test_the_audit_records_the_refusal_the_frozen_spec_made(self, monkeypatch, tmp_path):
        """The live file changing after the freeze must not turn a refused server
        into a supplied one in the trail: the record is the decision delivery used."""
        frozen = self._frozen_then_relaxed(monkeypatch, tmp_path)
        records = self._sel(monkeypatch)
        assert self._frozen_session(tmp_path, frozen) == []
        denied = [r for r in records if r.get("operation") == "mcp_declined_home_withheld"]
        assert [r["resources"] for r in denied] == [
            "outlook withheld from kiro session-level delivery"
        ], records
        assert "governance ceiling" in denied[0]["error"]
        supplied = [r for r in records if r.get("operation") == "mcp_declined_home_supplied"]
        assert not any(r["resources"].startswith("outlook") for r in supplied), supplied

    def test_the_audit_records_the_supply_the_frozen_spec_made(self, monkeypatch, tmp_path):
        """The other direction: a frozen spec that permits the server, a live file
        that would not. The session gets it, and the trail says so."""
        from kiro_crew import agent, mcp_declined_home

        shared = _declined_home(monkeypatch, tmp_path, {"outlook": {"command": _CMD}})
        frozen = dict(_DEFAULT_HOME_SPEC, allowedTools=[])
        _write_spec(shared, frozen)
        monkeypatch.setattr(mcp_declined_home, "may_skip_gate_now", lambda _ref: False)
        agent.rebuild_agent_config()
        _write_spec(shared, dict(_DEFAULT_HOME_SPEC, allowedTools=["*"]))
        records = self._sel(monkeypatch)
        assert [e["name"] for e in self._frozen_session(tmp_path, frozen)] == ["outlook"]
        assert not [r for r in records if r.get("operation") == "mcp_declined_home_withheld"]
        supplied = [r for r in records if r.get("operation") == "mcp_declined_home_supplied"]
        assert len(supplied) == 1 and supplied[0]["resources"].startswith("outlook supplied")


class TestOneDecisionRecord:
    """Every refusal and every supply is recorded from the one decision record a
    session start builds, and only one function per channel reads a spec."""

    @pytest.mark.parametrize("fails", ["sel_init", "submit"])
    def test_a_failed_sel_write_is_retried_by_the_next_identical_decision(
        self, monkeypatch, tmp_path, fails
    ):
        """A write that failed is not marked recorded: the next identical session
        start writes both the refusal and the supply."""
        from kiro_crew import agent

        _declined_home(
            monkeypatch,
            tmp_path,
            {
                "outlook": {"command": _CMD},
                "remote": {"url": "https://example.invalid/mcp"},
            },
        )
        agent.rebuild_agent_config()
        records: list[dict] = []
        broken = {"on": True}

        class _Sel:
            def log_api_access(self, **kw):
                if broken["on"] and fails == "submit":
                    raise PermissionError("sel unavailable")
                if str(kw.get("operation", "")).startswith("mcp_declined_home_"):
                    records.append(kw)

        def _sel():
            if broken["on"] and fails == "sel_init":
                raise PermissionError("sel unavailable")
            return _Sel()

        monkeypatch.setattr(agent, "sel", _sel)
        assert [e["name"] for e in _kiro_session_array(tmp_path)] == ["outlook"]
        assert records == []
        broken["on"] = False
        assert [e["name"] for e in _kiro_session_array(tmp_path)] == ["outlook"]
        ops = sorted((r["operation"], r["resources"]) for r in records)
        assert ops == [
            (
                "mcp_declined_home_supplied",
                "outlook supplied to kiro session-level delivery (refused shared home)",
            ),
            ("mcp_declined_home_withheld", "remote withheld from kiro session-level delivery"),
        ], records
        # Once written, the same decision is not recorded again.
        _kiro_session_array(tmp_path)
        assert len(records) == 2, records

    def _kas(self, tmp_path) -> None:
        from kiro_crew import agent, mcp_declined_home

        spec = json.loads(
            (agent.kiro_agents_dir_path() / "kirocrew.json").read_text(encoding="utf-8")
        )
        mcp_declined_home.kas_tool_grants("kirocrew", spec=spec, work_dir=tmp_path / "work")

    def _scenario(self, monkeypatch, tmp_path, kind: str) -> None:
        from kiro_crew import agent, mcp_declined_home

        on_kas = kind.startswith("kas")
        kind = kind.removeprefix("kas:")
        servers: dict = {"outlook": {"command": _CMD}}
        if kind == "remote":
            servers = {"outlook": {"url": "https://example.invalid/mcp"}}
        elif kind == "restricted":
            servers = {"outlook": {"command": _CMD, "disabledTools": ["send_mail"]}}
        elif kind == "oversized":
            servers = {"a": {"command": _CMD}, "outlook": {"command": _CMD}}
            monkeypatch.setattr(mcp_declined_home, "MAX_DELIVERED_SERVERS", 1)
        shared = _declined_home(monkeypatch, tmp_path, servers)
        if kind == "ceiling":
            _write_spec(shared, dict(_DEFAULT_HOME_SPEC, allowedTools=["*"]))
            monkeypatch.setattr(mcp_declined_home, "may_skip_gate_now", lambda _ref: False)
        agent.rebuild_agent_config()
        if kind == "unreadable_spec":
            monkeypatch.setattr(mcp_declined_home, "_shared_spec_view", lambda: None)
        if kind == "kas_not_native":
            monkeypatch.setattr(mcp_declined_home, "_kas_native_names", lambda *_a: frozenset())
            mcp_declined_home.clear_projection()
        if kind in ("kiro_error", "kas_error"):

            def _boom(*_a, **_k):
                raise RuntimeError("boom")

            monkeypatch.setattr(mcp_declined_home, "_refresh", _boom)
        if on_kas:
            if kind == "kas_workspace":
                _checkout_declares(tmp_path / "work", {"outlook": {"command": "other"}})
            self._kas(tmp_path)
        else:
            _kiro_session_array(tmp_path)

    @pytest.mark.parametrize(
        "kind",
        [
            "remote",
            "restricted",
            "oversized",
            "ceiling",
            "unreadable_spec",
            "kiro_error",
            "kas:remote",
            "kas:restricted",
            "kas:oversized",
            "kas_workspace",
            "kas_not_native",
            "kas_error",
        ],
    )
    def test_every_refusal_is_recorded_from_the_decision(self, monkeypatch, tmp_path, kind):
        from kiro_crew import agent, mcp_declined_home

        sel: list[dict] = []

        class _Sel:
            def log_api_access(self, **kw):
                if str(kw.get("operation", "")).startswith("mcp_declined_home_"):
                    sel.append(kw)

        monkeypatch.setattr(agent, "sel", lambda: _Sel())
        decisions: list = []
        monkeypatch.setattr(mcp_declined_home, "_record_decision", decisions.append)
        self._scenario(monkeypatch, tmp_path, kind)
        # With the decision's recorder taken away, nothing else records anything.
        assert sel == [], sel
        scope = (
            "KAS session tool grants" if kind.startswith("kas") else ("kiro session-level delivery")
        )
        bare = kind.removeprefix("kas:")
        refused = {
            r.kind: r.names.count
            for d in decisions
            if d.scope == scope
            for r in d.refusals
            if r.names.count
        }
        assert refused.get(bare, 0) >= 1, (kind, refused)

    def test_a_kas_session_records_a_server_the_projection_refused(self, monkeypatch, tmp_path):
        """GPT's case end to end: a disabledTools server on KAS is withheld, and
        SEL says so under the KAS channel."""
        from kiro_crew import agent

        records: list[dict] = []

        class _Sel:
            def log_api_access(self, **kw):
                if kw.get("operation") == "mcp_declined_home_withheld":
                    records.append(kw)

        _declined_home(
            monkeypatch,
            tmp_path,
            {"outlook": {"command": _CMD, "disabledTools": ["send_mail"]}},
        )
        agent.rebuild_agent_config()
        monkeypatch.setattr(agent, "sel", lambda: _Sel())
        self._kas(tmp_path)
        self._kas(tmp_path)
        assert [r["resources"] for r in records] == [
            "outlook withheld from KAS session tool grants"
        ], records
        assert "disabledTools" in records[0]["error"]
        # The same refusal on kiro is its own record, not deduped against KAS.
        _kiro_session_array(tmp_path)
        assert [r["resources"] for r in records][1:] == [
            "outlook withheld from kiro session-level delivery"
        ], records
        # Alternating channels does not re-record either one.
        self._kas(tmp_path)
        _kiro_session_array(tmp_path)
        assert len(records) == 2, records

    def test_only_the_decision_builders_read_a_spec_or_record(self):
        """Pin the shape: one spec reader per kiro session start, and every SEL
        record written by the decision recorder alone -- plus the mode-switch
        check, which reads only the spec the switch activates, rebuilds no
        decision and writes only the record of ending the session."""
        import ast
        import inspect

        from kiro_crew import mcp_declined_home

        tree = ast.parse(inspect.getsource(mcp_declined_home))
        callers: dict[str, set[str]] = {}
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    callers.setdefault(node.func.id, set()).add(fn.name)
        assert callers["_shared_spec_view"] == {"_kiro_decision", "replacement_refusal"}
        assert callers["_spec_view"] == {
            "_kiro_decision",
            "_shared_spec_view",
            "replacement_refusal",
        }
        assert callers["_audit_refusal"] == {"_record_decision"}
        assert callers["_audit_supplied"] == {"_record_decision"}
        assert callers["_record_withheld"] == {"_audit_refusal", "replacement_refusal"}
        # The mode switch never builds or records a decision.
        assert "replacement_refusal" not in callers["_kiro_decision"]
        assert "replacement_refusal" not in callers["_record_decision"]


class TestModeSwitchKeepsTheDecision:
    """A mode switch activates a newer spec but delivers nothing: the decision the
    session started under stays its record, for delivery and audit alike, and the
    switch is judged against it rather than deciding again."""

    _VIEW = "kirocrew-skill-view-" + "c" * 24

    def _decided(self, monkeypatch, tmp_path) -> tuple[object, list[dict]]:
        """Start a session on a frozen spec that pre-approves ``calendar`` only.

        The ceiling refuses every grant, so ``calendar`` is refused and ``outlook``
        supplied. Returns the decision and the live SEL capture."""
        from kiro_crew import agent, mcp_declined_home

        shared = _declined_home(
            monkeypatch,
            tmp_path,
            {"outlook": {"command": _CMD}, "calendar": {"command": _CMD}},
        )
        frozen = dict(_DEFAULT_HOME_SPEC, allowedTools=["@calendar"])
        _write_spec(shared, frozen)
        monkeypatch.setattr(mcp_declined_home, "may_skip_gate_now", lambda _ref: False)
        agent.rebuild_agent_config()
        records: list[dict] = []

        class _Sel:
            def log_api_access(self, **kw):
                records.append(kw)

        monkeypatch.setattr(agent, "sel", lambda: _Sel())
        elements, decision = mcp_declined_home.session_delivery(
            "kirocrew", spec=json.loads(json.dumps(frozen))
        )
        assert [e["name"] for e in elements] == ["outlook"]
        assert decision is not None and decision.supplied == ("outlook",)
        return decision, records

    @staticmethod
    def _ours(records: list[dict]) -> list[dict]:
        return [r for r in records if str(r.get("operation", "")).startswith("mcp_declined_home")]

    def _projection(self, allowed: list[str]):
        from kiro_crew.acp.skill_projection import NativeSkillProjection

        return NativeSkillProjection(
            {"kirocrew": self._VIEW},
            specs={"kirocrew": dict(_DEFAULT_HOME_SPEC, allowedTools=allowed)},
        )

    def test_a_switch_that_would_supply_a_refused_server_keeps_the_refusal(
        self, monkeypatch, tmp_path
    ):
        """The replacement spec does not pre-approve ``calendar``, so a decision
        rebuilt from it would supply it; the switch neither delivers it nor
        records it as supplied."""
        from kiro_crew import mcp_declined_home

        decision, records = self._decided(monkeypatch, tmp_path)
        before = list(self._ours(records))
        assert any("calendar" in r["resources"] for r in before if r["outcome"] == "denied")
        assert (
            mcp_declined_home.replacement_refusal(decision, self._projection([]), "kirocrew")
            is None
        )
        assert decision.supplied == ("outlook",)
        assert self._ours(records) == before

    def test_a_switch_that_pre_approves_a_delivered_server_is_refused_and_recorded(
        self, monkeypatch, tmp_path
    ):
        from kiro_crew import mcp_declined_home

        decision, records = self._decided(monkeypatch, tmp_path)
        before = len(self._ours(records))
        refusal = mcp_declined_home.replacement_refusal(
            decision, self._projection(["@outlook"]), "kirocrew"
        )
        assert refusal and "outlook" in refusal
        new = self._ours(records)[before:]
        assert [(r["outcome"], r["resources"]) for r in new] == [
            ("denied", "outlook withheld from a kiro session's mode switch")
        ], new
        assert "governance ceiling" in new[0]["error"]
        # The decision is not rebuilt: what the session was given is unchanged.
        assert decision.supplied == ("outlook",)

    @pytest.mark.parametrize("case", ["no-view", "other-agent-without-projection"])
    def test_a_replacement_spec_that_cannot_be_read_is_refused(self, monkeypatch, tmp_path, case):
        from kiro_crew import mcp_declined_home

        decision, records = self._decided(monkeypatch, tmp_path)
        before = len(self._ours(records))
        if case == "no-view":
            refusal = mcp_declined_home.replacement_refusal(
                decision, SimpleNamespace(specs={}), "kirocrew"
            )
        else:
            refusal = mcp_declined_home.replacement_refusal(decision, None, "ops")
        assert refusal and "unreadable" in refusal
        assert [r["outcome"] for r in self._ours(records)[before:]] == ["denied"]

    def test_a_session_delivered_nothing_is_never_refused(self):
        from kiro_crew import mcp_declined_home

        assert mcp_declined_home.replacement_refusal(None, None, "ops") is None

    def _bracket(self, monkeypatch, allowed: list[str], decision) -> tuple[object, object, object]:
        """Drive the REAL start bracket against a fake kiro-cli whose re-prepared
        view carries *allowed*. Returns (runtime, fake kiro, outcome)."""
        import asyncio
        from unittest.mock import patch

        from test_acp_set_mode_projection_race import _runtime

        from kiro_crew.acp import runtime as runtime_mod
        from kiro_crew.acp.session_handle import AcpRuntimeError

        monkeypatch.setattr(runtime_mod, "_PROJECTED_MODE_RETRY_DELAYS_SECS", (0.0, 0.0))
        replacement = self._projection(allowed)
        made: dict[str, object] = {}

        async def drive():
            # Built on this loop: the fake's stream reader binds to the loop it
            # was created on.
            rt, kiro = _runtime({self._VIEW})
            rt._agent = "kirocrew"
            made.update(rt=rt, kiro=kiro)
            reader = asyncio.ensure_future(rt._reader_loop())
            try:
                with (
                    patch(
                        "kiro_crew.acp.skill_projection.prepare_native_skill_projection",
                        side_effect=lambda *_a, **_k: replacement,
                    ),
                    patch("kiro_crew.agent.require_unchanged_derived_spec", return_value=None),
                ):
                    return await rt._activate_mode_bracketed(
                        "s1",
                        "kirocrew",
                        budget=5.0,
                        payload_snapshot=None,
                        wire_registered=True,
                        declined_decision=decision,
                    )
            except AcpRuntimeError as exc:  # the outcome is what the test judges
                return exc
            finally:
                reader.cancel()

        outcome = asyncio.run(drive())
        return made["rt"], made["kiro"], outcome

    def test_the_start_bracket_ends_a_session_its_switch_would_pre_approve(
        self, monkeypatch, tmp_path
    ):
        """The finding's shape: delivery judged the earlier view, the bracket
        re-prepares one that approves the delivered server. The session ends
        before ``set_mode`` and the trail records why, not a new supply."""
        from kiro_crew.acp.session_handle import AcpRuntimeError

        decision, records = self._decided(monkeypatch, tmp_path)
        before = len(self._ours(records))
        rt, kiro, outcome = self._bracket(monkeypatch, ["@outlook"], decision)
        assert isinstance(outcome, AcpRuntimeError), outcome
        assert "outlook" in str(outcome)
        assert kiro.set_modes == []
        rt.terminate_session.assert_awaited_with("s1")
        assert [r["outcome"] for r in self._ours(records)[before:]] == ["denied"]

    def test_the_start_bracket_keeps_the_decision_across_a_permitted_switch(
        self, monkeypatch, tmp_path
    ):
        """A replacement that would turn ``calendar`` from refused to supplied
        activates, and neither delivers nor records anything new."""
        decision, records = self._decided(monkeypatch, tmp_path)
        before = list(self._ours(records))
        rt, kiro, outcome = self._bracket(monkeypatch, [], decision)
        assert outcome == self._VIEW, outcome
        assert kiro.set_modes == [self._VIEW]
        rt.terminate_session.assert_not_awaited()
        assert self._ours(records) == before
        assert decision.supplied == ("outlook",)

    def _handle(self, tmp_path, allowed: list[str], decision):
        import asyncio
        from unittest.mock import AsyncMock

        from kiro_crew.acp.runtime import AcpRuntime
        from kiro_crew.acp.session_handle import AcpSessionHandle

        rt = AcpRuntime(work_dir=str(tmp_path))
        rt._native_skill_projection = self._projection(allowed)
        rt.send_request = AsyncMock()  # type: ignore[method-assign]
        rt.terminate_session = AsyncMock()  # type: ignore[method-assign]
        handle = AcpSessionHandle(session_id="s1", queue=asyncio.Queue(), runtime=rt)
        handle.declined_decision = decision
        return rt, handle

    def test_a_mid_session_switch_is_judged_against_the_decision(self, monkeypatch, tmp_path):
        import asyncio

        from kiro_crew.acp.session_handle import AcpRuntimeError

        decision, _records = self._decided(monkeypatch, tmp_path)
        rt, handle = self._handle(tmp_path, ["@outlook"], decision)
        with pytest.raises(AcpRuntimeError, match="outlook"):
            asyncio.run(handle.set_mode("kirocrew"))
        rt.send_request.assert_not_awaited()

        rt, handle = self._handle(tmp_path, [], decision)
        asyncio.run(handle.set_mode("kirocrew"))
        rt.send_request.assert_awaited_once()
        rt.terminate_session.assert_not_awaited()

    def test_a_view_adopted_during_the_send_ends_the_session(self, monkeypatch, tmp_path):
        import asyncio

        from kiro_crew.acp.session_handle import AcpRuntimeError

        decision, _records = self._decided(monkeypatch, tmp_path)
        rt, handle = self._handle(tmp_path, [], decision)
        approving = self._projection(["@outlook"])

        async def adopt(*_a, **_k):
            rt._native_skill_projection = approving

        rt.send_request.side_effect = adopt
        with pytest.raises(AcpRuntimeError, match="outlook"):
            asyncio.run(handle.set_mode("kirocrew"))
        rt.terminate_session.assert_awaited_once_with("s1")

    def test_both_start_paths_carry_the_decision_to_the_switch_and_the_handle(self):
        """The decision a start delivered under reaches its bracket and its handle
        on create, late adoption and load alike -- never a ``None`` that would
        let the switch go unjudged."""
        import ast
        import inspect

        from kiro_crew.acp import runtime

        tree = ast.parse(inspect.getsource(runtime))
        calls: dict[str, list[ast.Call]] = {}
        assigned: list[str] = []
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("_activate_mode_bracketed", "_finish_create_session")
                ):
                    calls.setdefault(fn.name, []).append(node)
                if (
                    isinstance(node, ast.Assign)
                    and isinstance(node.targets[0], ast.Attribute)
                    and node.targets[0].attr == "declined_decision"
                ):
                    assigned.append(fn.name)
        carried = {
            (name, call.func.attr)  # type: ignore[attr-defined]
            for name, found in calls.items()
            for call in found
            if any(
                k.arg == "declined_decision"
                and isinstance(k.value, ast.Name)
                and k.value.id == "declined_decision"
                for k in call.keywords
            )
        }
        every = {(name, call.func.attr) for name, found in calls.items() for call in found}  # type: ignore[attr-defined]
        assert (
            carried == every
            and {
                ("_finish_create_session", "_activate_mode_bracketed"),
                ("load_session", "_activate_mode_bracketed"),
                ("create_session", "_finish_create_session"),
                ("_adopt", "_finish_create_session"),
            }
            <= every
        ), (every, carried)
        assert sorted(assigned) == ["_finish_create_session", "load_session"]


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
        _kiro_session_array(tmp_path)
        _kiro_session_array(tmp_path)
        # A rebuild-side refresh supplies no session, so it records nothing.
        mcp_declined_home.refresh_projection()
        supplied = [r for r in records if r.get("operation") == "mcp_declined_home_supplied"]
        assert len(supplied) == 1, records
        assert "outlook" in supplied[0]["resources"]
        assert "kiro session-level delivery" in supplied[0]["resources"]

    def test_withdrawing_the_last_server_writes_a_record(self, monkeypatch, tmp_path):
        """Removals are tool-surface changes too: the trail must not show only additions."""
        import os

        from kiro_crew import agent

        _declined_home(monkeypatch, tmp_path, {"outlook": {"command": _CMD}})
        records: list[dict] = []

        class _Sel:
            def log_api_access(self, **kw):
                records.append(kw)

        monkeypatch.setattr(agent, "sel", lambda: _Sel())
        assert [e["name"] for e in _kiro_session_array(tmp_path)] == ["outlook"]
        settings = tmp_path / "settings-mcp.json"
        settings.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        st = settings.stat()
        os.utime(settings, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        assert _kiro_session_array(tmp_path) == []
        supplied = [r for r in records if r.get("operation") == "mcp_declined_home_supplied"]
        assert len(supplied) == 2, records
        assert "withdrawn: outlook" in supplied[1]["resources"], supplied[1]


class TestEveryRefusalIsAudited:
    """Each refusal writes the ceiling's ``mcp_declined_home_withheld`` record,
    once per change to what it refuses, not once per session."""

    def _denied(self, monkeypatch) -> list[dict]:
        from kiro_crew import agent

        records: list[dict] = []

        class _Sel:
            def log_api_access(self, **kw):
                if kw.get("operation") == "mcp_declined_home_withheld":
                    records.append(kw)

        monkeypatch.setattr(agent, "sel", lambda: _Sel())
        return records

    def _kas_grants(self, tmp_path) -> list[str]:
        from kiro_crew import agent as agent_mod
        from kiro_crew import mcp_declined_home
        from kiro_crew.agent_files import AGENT_FILENAME

        spec = json.loads(
            (agent_mod.kiro_agents_dir_path() / AGENT_FILENAME).read_text(encoding="utf-8")
        )
        return mcp_declined_home.kas_tool_grants("kirocrew", spec=spec, work_dir=tmp_path / "work")

    def test_a_kas_grant_withheld_for_a_workspace_name_writes_one_record(
        self, monkeypatch, tmp_path
    ):
        from kiro_crew import agent

        _declined_home(
            monkeypatch, tmp_path, {"outlook": {"command": _CMD}, "calendar": {"command": _CMD}}
        )
        agent.rebuild_agent_config()
        _checkout_declares(tmp_path / "work", {"outlook": {"command": "other"}})
        denied = self._denied(monkeypatch)
        assert self._kas_grants(tmp_path) == ["@calendar"]
        assert self._kas_grants(tmp_path) == ["@calendar"]
        assert len(denied) == 1, denied
        assert denied[0]["outcome"] == "denied"
        assert denied[0]["resources"] == "outlook withheld from KAS session tool grants"
        assert "workspace mcp.json" in denied[0]["error"]

    def test_refusals_whose_shown_names_match_still_both_record(self, monkeypatch, tmp_path):
        """The record shows names cut to 64 characters; two different refusals that
        agree on that cut are still two refusals."""
        from kiro_crew import agent, mcp_declined_home

        first, second = "a" * 64 + "-one", "a" * 64 + "-two"
        _declined_home(monkeypatch, tmp_path, {first: {"command": _CMD}, second: {"command": _CMD}})
        agent.rebuild_agent_config()
        denied = self._denied(monkeypatch)
        spec = json.loads(
            (agent.kiro_agents_dir_path() / "kirocrew.json").read_text(encoding="utf-8")
        )
        for name, work in ((first, "w1"), (second, "w2")):
            _checkout_declares(tmp_path / work, {name: {"command": "other"}})
            mcp_declined_home.kas_tool_grants("kirocrew", spec=spec, work_dir=tmp_path / work)
        assert len(denied) == 2, denied
        assert denied[0]["resources"] == denied[1]["resources"]

    @pytest.mark.parametrize(
        ("servers", "named", "reason"),
        [
            ({"remote": {"url": "https://example.invalid/mcp"}}, "remote", "remote server"),
            (
                {"outlook": {"command": _CMD, "disabledTools": ["send_mail"]}},
                "outlook",
                "disabledTools",
            ),
            ({"a": {"command": _CMD}, "b": {"command": _CMD}}, "b", "projection bounds"),
        ],
    )
    def test_a_server_the_projection_refuses_writes_one_record(
        self, monkeypatch, tmp_path, servers, named, reason
    ):
        from kiro_crew import mcp_declined_home

        monkeypatch.setattr(mcp_declined_home, "MAX_DELIVERED_SERVERS", 1)
        _declined_home(monkeypatch, tmp_path, servers)
        denied = self._denied(monkeypatch)
        _kiro_session_array(tmp_path)
        _kiro_session_array(tmp_path)
        assert len(denied) == 1, denied
        assert denied[0]["resources"] == f"{named} withheld from kiro session-level delivery"
        assert reason in denied[0]["error"]

    def test_an_unreadable_shared_spec_writes_a_record(self, monkeypatch, tmp_path):
        from kiro_crew import mcp_declined_home

        _declined_home(monkeypatch, tmp_path, {"outlook": {"command": _CMD}})
        monkeypatch.setattr(mcp_declined_home, "_shared_spec_view", lambda: None)
        denied = self._denied(monkeypatch)
        assert _kiro_session_array(tmp_path) == []
        assert [d["resources"] for d in denied] == [
            "outlook withheld from kiro session-level delivery"
        ]

    def test_a_failed_delivery_or_grant_writes_a_record(self, monkeypatch, tmp_path):
        from kiro_crew import mcp_declined_home

        def _boom(*_a, **_k):
            raise RuntimeError("boom")

        monkeypatch.setattr(mcp_declined_home, "_session_servers", _boom)
        monkeypatch.setattr(mcp_declined_home, "_kas_tool_grants", _boom)
        denied = self._denied(monkeypatch)
        assert mcp_declined_home.session_servers("kirocrew") == []
        assert mcp_declined_home.kas_tool_grants("kirocrew", spec={}) == []
        assert [d["resources"] for d in denied] == [
            "every mcp.json server withheld from kiro session-level delivery",
            "every mcp.json server withheld from KAS session tool grants",
        ]


class TestTimeoutCarriage:
    def test_a_server_with_a_timeout_is_still_delivered_and_the_drop_is_named(
        self, monkeypatch, tmp_path, caplog
    ):
        """An ACP element has no timeout field; withholding the server would be
        the original bug, so it is delivered and the lost timeout is logged."""
        from kiro_crew import agent

        _declined_home(
            monkeypatch,
            tmp_path,
            {"outlook": {"command": _CMD, "timeout": 300000}, "calendar": {"command": _CMD}},
        )
        with caplog.at_level("WARNING", logger="kiro_crew.mcp_declined_home"):
            agent.rebuild_agent_config()
            names = [e["name"] for e in _kiro_session_array(tmp_path)]

        assert names == ["calendar", "outlook"], names
        warned = [r.getMessage() for r in caplog.records if "default timeout" in r.getMessage()]
        assert len(warned) == 1 and "outlook" in warned[0] and "calendar" not in warned[0]

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


async def _kas_custom_agent(
    monkeypatch, shared: Path, work_dir: Path, stubbed=frozenset(), session_work_dir=None
):
    """The ``customAgents`` entry a KAS ``session/new`` carries, built by the harness.

    *work_dir* is the runtime's directory; *session_work_dir*, when given, is the
    directory the session itself opens in.
    """
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
    kwargs = {}
    if session_work_dir is not None:
        Path(session_work_dir).mkdir(parents=True, exist_ok=True)
        kwargs["session_work_dir"] = str(session_work_dir)
    extras = await KasHarness().session_extras("kirocrew", work_dir=str(work_dir), **kwargs)
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
        assert await AcpRuntime._with_declined_home_servers(fake, [], None, tmp_path) == ([], None)

    @pytest.mark.asyncio
    async def test_a_writable_home_projects_unchanged(self, monkeypatch, tmp_path):
        """Nothing is granted while this instance owns its spec."""
        from kiro_crew import agent

        shared = _declined_home(monkeypatch, tmp_path, {"outlook": {"command": _CMD}})
        monkeypatch.setattr(agent, "_decline_shared_agent_home", lambda audit=True: None)

        custom = await _kas_custom_agent(monkeypatch, shared, tmp_path / "work")
        assert "@outlook" not in custom["tools"]

    @pytest.mark.asyncio
    async def test_a_per_tool_allowlist_is_not_widened_to_the_whole_server(
        self, monkeypatch, tmp_path
    ):
        """``tools: [@outlook/read_mail]`` under ``allowedTools: [@outlook]``: the
        harness must not append ``@outlook``, so ``send_mail`` stays ungranted."""
        from kiro_crew import agent

        shared = _declined_home(monkeypatch, tmp_path, {"outlook": {"command": _CMD}})
        agent.rebuild_agent_config()
        spec = dict(_DEFAULT_HOME_SPEC, allowedTools=["@outlook"])
        spec["tools"] = [*spec["tools"], "@outlook/read_mail"]
        _write_spec(shared, spec)

        custom = await _kas_custom_agent(monkeypatch, shared, tmp_path / "work")
        assert "@outlook" not in custom["tools"], custom["tools"]
        assert custom["tools"].count("@outlook/read_mail") == 1
        assert not any(
            t == "@outlook/send_mail" or (t.startswith("@outlook") and "/" not in t)
            for t in custom["tools"]
        )

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

    def test_a_per_tool_ref_withholds_the_whole_server_grant(self, monkeypatch, tmp_path):
        """``@outlook/read_mail`` is the operator's allowlist: a whole-server
        ``@outlook`` on top would expose ``send_mail`` under an ``allowedTools``
        ceiling that permits ``@outlook``."""
        from kiro_crew import agent

        shared = _declined_home(monkeypatch, tmp_path, {"outlook": {"command": _CMD}})
        _write_spec(shared, dict(_DEFAULT_HOME_SPEC, allowedTools=["@outlook"]))
        agent.rebuild_agent_config()
        assert self._grants(tmp_path, tools=["fs_read", "@outlook/read_mail"]) == []

    def test_a_glob_per_tool_ref_withholds_the_whole_server_grant(self, monkeypatch, tmp_path):
        from kiro_crew import agent

        _declined_home(
            monkeypatch, tmp_path, {"outlook": {"command": _CMD}, "calendar": {"command": _CMD}}
        )
        agent.rebuild_agent_config()
        assert self._grants(tmp_path, tools=["fs_read", "@out*/read_mail"]) == ["@calendar"]
        assert self._grants(tmp_path, tools=["fs_read", "@outlook/*_mail"]) == ["@calendar"]

    def test_a_per_tool_ref_leaves_another_server_granted(self, monkeypatch, tmp_path):
        from kiro_crew import agent

        _declined_home(
            monkeypatch, tmp_path, {"outlook": {"command": _CMD}, "calendar": {"command": _CMD}}
        )
        agent.rebuild_agent_config()
        assert self._grants(tmp_path, tools=["fs_read", "@calendar/list"]) == ["@outlook"]
        assert self._grants(tmp_path, tools=["fs_read", "@outlook"]) == ["@calendar"]

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
        assert out["tools"] == ["fs_read", "read_file", "list_directory", "@outlook", "@calendar"]
        assert (
            to_client_custom_agent(
                "a", {"name": "a", "tools": "*"}, "p", extra_tool_refs=["@outlook"]
            )["tools"]
            == "*"
        )


def _checkout_declares(checkout: Path, servers: dict) -> None:
    settings = checkout / ".kiro" / "settings"
    settings.mkdir(parents=True, exist_ok=True)
    (settings / "mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


class TestKasGrantReadsTheSessionCheckout:
    """The runtime starts in directory A; a session can open in directory B. KAS mounts
    B's ``.kiro/settings/mcp.json`` for that session, so the same-name check must read B.
    Reading A would grant ``@outlook`` -- and with ``allowedTools: [@outlook]`` its
    inherited approval -- to a server the untrusted checkout B declares."""

    @pytest.mark.asyncio
    async def test_a_same_name_server_in_the_session_checkout_withholds_the_grant(
        self, monkeypatch, tmp_path
    ):
        from kiro_crew import agent

        shared = _declined_home(monkeypatch, tmp_path, {"outlook": {"command": _CMD}})
        agent.rebuild_agent_config()
        _write_spec(shared, dict(_DEFAULT_HOME_SPEC, allowedTools=["@outlook"]))
        runtime_dir, session_dir = tmp_path / "A", tmp_path / "B"
        _checkout_declares(session_dir, {"outlook": {"command": _CMD, "args": ["--b"]}})

        custom = await _kas_custom_agent(
            monkeypatch, shared, runtime_dir, session_work_dir=session_dir
        )
        assert "@outlook" not in custom["tools"], custom["tools"]

    @pytest.mark.asyncio
    async def test_a_session_checkout_without_a_collision_still_gets_the_grant(
        self, monkeypatch, tmp_path
    ):
        from kiro_crew import agent

        shared = _declined_home(monkeypatch, tmp_path, {"outlook": {"command": _CMD}})
        agent.rebuild_agent_config()
        runtime_dir, session_dir = tmp_path / "A", tmp_path / "B"
        _checkout_declares(session_dir, {"other": {"command": _CMD}})

        custom = await _kas_custom_agent(
            monkeypatch, shared, runtime_dir, session_work_dir=session_dir
        )
        assert custom["tools"].count("@outlook") == 1, custom["tools"]

    @pytest.mark.asyncio
    async def test_a_collision_only_in_the_runtime_dir_does_not_follow_the_session(
        self, monkeypatch, tmp_path
    ):
        """B is the session's checkout; A's declaration is not what KAS mounts for it."""
        from kiro_crew import agent

        shared = _declined_home(monkeypatch, tmp_path, {"outlook": {"command": _CMD}})
        agent.rebuild_agent_config()
        runtime_dir, session_dir = tmp_path / "A", tmp_path / "B"
        _checkout_declares(runtime_dir, {"outlook": {"command": _CMD}})

        custom = await _kas_custom_agent(
            monkeypatch, shared, runtime_dir, session_work_dir=session_dir
        )
        assert custom["tools"].count("@outlook") == 1, custom["tools"]

    @pytest.mark.asyncio
    async def test_without_a_session_dir_the_runtime_dir_is_checked_as_before(
        self, monkeypatch, tmp_path
    ):
        from kiro_crew import agent

        shared = _declined_home(monkeypatch, tmp_path, {"outlook": {"command": _CMD}})
        agent.rebuild_agent_config()
        runtime_dir = tmp_path / "A"
        _checkout_declares(runtime_dir, {"outlook": {"command": _CMD}})

        custom = await _kas_custom_agent(monkeypatch, shared, runtime_dir)
        assert "@outlook" not in custom["tools"], custom["tools"]

    @pytest.mark.asyncio
    async def test_the_runtime_hands_the_session_dir_to_the_harness(self, tmp_path):
        from kiro_crew.acp.harness import SessionExtras
        from kiro_crew.acp.runtime import AcpRuntime

        seen: dict = {}

        class _Harness:
            async def session_extras(self, agent, **kw):
                seen.update(kw)
                return SessionExtras()

        rt = object.__new__(AcpRuntime)
        rt._harness_resolved = _Harness()
        rt._work_dir = tmp_path / "A"
        rt._mcp_gateway_overlay = None
        rt._refuse_if_loader_unreachable = lambda agent, custom: None
        await rt._kas_custom_agents("kirocrew", session_work_dir=str(tmp_path / "B"))
        assert seen["work_dir"] == tmp_path / "A"
        assert seen["session_work_dir"] == str(tmp_path / "B")

    @pytest.mark.parametrize("method", ["create_session", "load_session"])
    def test_both_session_start_paths_pass_their_own_dir(self, method):
        """Each start path resolves ``session_work_dir`` for the session; both must hand
        that one, not the runtime's, to the projection that builds the grant."""
        import ast
        import inspect
        import textwrap

        from kiro_crew.acp.runtime import AcpRuntime

        tree = ast.parse(textwrap.dedent(inspect.getsource(getattr(AcpRuntime, method))))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_kas_custom_agents"
        ]
        assert len(calls) == 1, method
        passed = {kw.arg: kw.value for kw in calls[0].keywords}
        value = passed.get("session_work_dir")
        assert isinstance(value, ast.Name) and value.id == "session_work_dir", method
