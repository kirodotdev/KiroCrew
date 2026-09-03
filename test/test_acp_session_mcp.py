"""Session-array MCP wiring: agent spec -> session/new mcpServers array.

A backend in ``ACP_BACKENDS_SESSION_MCP_ARRAY`` (claude-agent-acp today) receives
its MCP servers ONLY through the ``session/new`` / ``session/load`` parameter, so
these tests pin the shape the adapter's schema requires (``env``/``headers`` always arrays, an explicit transport ``type``) and
the mounting rules the kiro agent spec expresses (``tools`` references, the
registry pointer, Crew's own control plane).
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from unittest import mock

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew.acp import client as client_mod
from kiro_crew.acp import session_mcp
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE
from kiro_crew.providers.mirrors import claude_code as claude_mirror
from kiro_crew.providers.mirrors import registry as mirrors_registry
from kiro_crew.providers.mirrors.claude_code import ClaudeCodeMirror

_CORE = {"command": "/opt/kirocrew", "args": ["mcp-core"]}
_CRON = {"command": "/opt/kirocrew", "args": ["mcp-cron"]}


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    """Point the agent-spec resolver at a temp agents directory."""
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", d)
    # Materialization would try to REBUILD the managed default from bundled
    # defaults; these tests supply the spec themselves.
    monkeypatch.setattr(session_mcp, "ensure_agent_materialized", lambda _a: True)
    monkeypatch.setattr(
        session_mcp,
        "managed_mcp_spec_entry",
        lambda name: {"kirocrew-core": dict(_CORE), "kirocrew-cron": dict(_CRON)}.get(name),
    )
    # Registry mode reads the effective config; pinned off (the default for a
    # personal install) so the symmetric filter is deterministic here. The tests
    # that care flip it explicitly.
    monkeypatch.setattr(session_mcp, "_mcp_registry_mode", lambda: False)
    return d


def _write_spec(agents_dir: Path, *, servers: dict, tools: list | None) -> None:
    spec: dict = {"name": "kirocrew", "mcpServers": servers}
    if tools is not None:
        spec["tools"] = tools
    (agents_dir / "kirocrew.json").write_text(json.dumps(spec), encoding="utf-8")


def _write_project_spec(project_dir: Path, *, servers: dict, tools: list | None) -> None:
    """A spec in the checkout kiro-cli resolves ``--agent`` against first."""
    agents = project_dir / ".kiro" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    spec: dict = {"name": "kirocrew", "mcpServers": servers}
    if tools is not None:
        spec["tools"] = tools
    (agents / "kirocrew.json").write_text(json.dumps(spec), encoding="utf-8")


def _by_name(elements: list[dict]) -> dict[str, dict]:
    return {e["name"]: e for e in elements}


class TestElementShape:
    def test_stdio_entry_carries_env_array_and_explicit_type(self, agents_dir):
        _write_spec(
            agents_dir,
            servers={"foo": {"command": "/bin/foo", "args": ["--x"], "env": {"K": "v"}}},
            tools=["@foo"],
        )
        foo = _by_name(session_mcp.session_mcp_servers("kirocrew"))["foo"]
        assert foo == {
            "name": "foo",
            "command": "/bin/foo",
            "args": ["--x"],
            # An array, not a mapping, and PRESENT even when empty: the adapter's
            # schema requires it and rejects the whole session/new otherwise.
            "env": [{"name": "K", "value": "v"}],
            "type": "stdio",
        }

    def test_stdio_entry_without_env_still_emits_the_array(self, agents_dir):
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        foo = _by_name(session_mcp.session_mcp_servers("kirocrew"))["foo"]
        assert foo["env"] == []
        assert foo["args"] == []

    def test_non_string_env_and_args_are_stringified(self, agents_dir):
        _write_spec(
            agents_dir,
            servers={"foo": {"command": "/bin/foo", "args": [7], "env": {"PORT": 8080}}},
            tools=["@foo"],
        )
        foo = _by_name(session_mcp.session_mcp_servers("kirocrew"))["foo"]
        assert foo["env"] == [{"name": "PORT", "value": "8080"}]
        assert foo["args"] == ["7"]

    def test_url_entry_defaults_to_http_with_headers_array(self, agents_dir):
        _write_spec(
            agents_dir,
            servers={"remote": {"url": "https://example.test/mcp", "headers": {"A": "b"}}},
            tools=["@remote"],
        )
        remote = _by_name(session_mcp.session_mcp_servers("kirocrew"))["remote"]
        assert remote == {
            "name": "remote",
            # Without an explicit type the adapter routes the entry to its stdio
            # branch and rejects it for having no command.
            "type": "http",
            "url": "https://example.test/mcp",
            "headers": [{"name": "A", "value": "b"}],
        }

    def test_url_entry_keeps_sse_transport(self, agents_dir):
        _write_spec(
            agents_dir,
            servers={"remote": {"url": "https://example.test/sse", "type": "sse"}},
            tools=["@remote"],
        )
        remote = _by_name(session_mcp.session_mcp_servers("kirocrew"))["remote"]
        assert remote["type"] == "sse"
        assert remote["headers"] == []

    @pytest.mark.parametrize("bad_args", [8080, "--flag", {"a": 1}, True])
    def test_non_sequence_args_does_not_raise(self, agents_dir, bad_args):
        """``"args": 8080`` must not take the whole session/new down.

        The spec is hand-editable JSON, so a scalar there is an easy mistake.
        Iterating it raises ``TypeError`` (or, for a string, explodes into one
        argument per character), and nothing in this module may raise: the
        exception travels out through ``session_mcp_servers`` and fails the whole
        ``session/new``, costing the session every OTHER server too.
        """
        _write_spec(
            agents_dir,
            servers={"foo": {"command": "/bin/foo", "args": bad_args}},
            tools=["@foo"],
        )
        assert _by_name(session_mcp.session_mcp_servers("kirocrew"))["foo"]["args"] == []

    def test_entry_with_no_transport_is_skipped(self, agents_dir):
        _write_spec(agents_dir, servers={"broken": {"args": ["--x"]}}, tools=["@broken"])
        assert "broken" not in _by_name(session_mcp.session_mcp_servers("kirocrew"))

    def test_kiro_only_keys_are_not_forwarded(self, agents_dir):
        _write_spec(
            agents_dir,
            servers={
                "foo": {
                    "command": "/bin/foo",
                    "timeout": 120,
                    "disabledTools": ["x"],
                    "autoApprove": ["y"],
                }
            },
            tools=["@foo"],
        )
        foo = _by_name(session_mcp.session_mcp_servers("kirocrew"))["foo"]
        # autoApprove above all: Claude's equivalent means Claude never asks, so
        # the call would never reach the host gate.
        assert set(foo) == {"name", "command", "args", "env", "type"}


class TestMounting:
    def test_server_not_referenced_by_tools_is_withheld(self, agents_dir):
        _write_spec(
            agents_dir,
            servers={"granted": {"command": "/bin/a"}, "ungranted": {"command": "/bin/b"}},
            tools=["@granted"],
        )
        names = _by_name(session_mcp.session_mcp_servers("kirocrew"))
        assert "granted" in names
        assert "ungranted" not in names

    def test_tool_scoped_reference_mounts_the_server(self, agents_dir):
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo/only_this"])
        assert "foo" in _by_name(session_mcp.session_mcp_servers("kirocrew"))

    def test_wildcard_reference_mounts_everything(self, agents_dir):
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["*"])
        assert "foo" in _by_name(session_mcp.session_mcp_servers("kirocrew"))

    def test_a_spec_without_tools_mounts_nothing(self, agents_dir):
        """A missing ``tools`` is an EMPTY allowlist, not "no filter".

        kiro-cli mounts a server only when ``tools`` names it, so a spec that
        references nothing grants nothing. Skipping the filter instead would mount
        every declared server -- including an ``opt_in`` one deliberately left
        unreferenced -- the moment the session ran on claude.
        """
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=None)
        assert "foo" not in _by_name(session_mcp.session_mcp_servers("kirocrew"))

    def test_a_non_list_tools_mounts_nothing(self, agents_dir):
        """The spec is hand-editable JSON, so `"tools": "@foo"` is an easy typo.

        Reading a scalar as "no allowlist" would mount every declared server off a
        malformed spec, which is the widest possible reading of the narrowest
        possible mistake. Fails closed instead.
        """
        for bad in ("@foo", 8080, {"foo": True}, True):
            _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=bad)
            assert "foo" not in _by_name(session_mcp.session_mcp_servers("kirocrew"))

    def test_at_wildcard_is_not_a_grant_all(self, agents_dir):
        # kiro documents `*`, `@builtin`, `@server` and `@server/tool` for
        # `tools`; `@*` parses as a server literally named `*`, so it mounts
        # NOTHING on kiro-cli. Reading it as grant-all here would mount every
        # declared server on this backend alone.
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@*"])
        assert "foo" not in _by_name(session_mcp.session_mcp_servers("kirocrew"))

    def test_registry_pointer_is_withheld_outside_registry_mode(self, agents_dir):
        _write_spec(
            agents_dir,
            servers={"governed": {"command": "/bin/ignored", "type": "registry"}},
            tools=["@governed"],
        )
        assert "governed" not in _by_name(session_mcp.session_mcp_servers("kirocrew"))

    def test_registry_mode_withholds_every_spec_declared_server(self, agents_dir, monkeypatch):
        """A marker cannot AUTHORIZE a server here, so a governed install gets none.

        kiro-cli resolves a marked entry against the admin's catalog by map key,
        drops what the catalog omits and applies the catalog's command override.
        Nothing here can do any of that -- only kiro-cli fetches the registry URL,
        and it persists neither the URL nor the catalog. A ``"type": "registry"``
        line is one a user can add to their own spec, so treating it as proof of
        authorization would let a local edit mount a server the administrator
        withheld. The unmarked entries are dropped for the reason they always
        were: kiro-cli drops them too.
        """
        _write_spec(
            agents_dir,
            servers={
                "marked": {"command": "/bin/marked", "type": "registry"},
                "local_only": {"command": "/bin/local"},
            },
            tools=["@marked", "@local_only"],
        )
        monkeypatch.setattr(session_mcp, "_mcp_registry_mode", lambda: True)
        names = _by_name(session_mcp.session_mcp_servers("kirocrew"))
        assert "marked" not in names
        assert "local_only" not in names

    def test_registry_mode_still_keeps_crews_own_control_plane(self, agents_dir, monkeypatch):
        """The withholding is scoped to the user-editable spec, not to the host.

        ``kirocrew-core``/``kirocrew-cron`` are re-derived from the managed source
        rather than read from the spec, and they are the session's only way to
        report back to its channel. Withholding them would reproduce the very
        defect this module exists to fix, on exactly the installs that are most
        governed.
        """
        _write_spec(
            agents_dir,
            servers={"marked": {"command": "/bin/marked", "type": "registry"}},
            tools=["@marked", "@kirocrew-core", "@kirocrew-cron"],
        )
        monkeypatch.setattr(session_mcp, "_mcp_registry_mode", lambda: True)
        names = _by_name(session_mcp.session_mcp_servers("kirocrew"))
        assert "kirocrew-core" in names
        assert "kirocrew-cron" in names

    def test_an_unreadable_registry_mode_is_read_as_governed(self, agents_dir, monkeypatch):
        """A ceiling that cannot be read is treated as in force, not as absent.

        Registry mode is a CEILING. Reading a config-plane failure as "off" would
        launch the session's unmarked local servers past a ceiling the operator may
        well have set -- the one outcome the ceiling exists to prevent. The cost is
        the session's spec-declared MCP surface, which is recoverable; mounting a
        server the administrator withheld is not.
        """

        def boom() -> bool:
            raise RuntimeError("config plane unreadable")

        _write_spec(
            agents_dir,
            servers={"local_only": {"command": "/bin/local"}},
            tools=["@local_only"],
        )
        monkeypatch.setattr(session_mcp, "_mcp_registry_mode", boom)
        assert "local_only" not in _by_name(session_mcp.session_mcp_servers("kirocrew"))

    def test_a_project_only_agents_allowlist_is_honoured(self, tmp_path, agents_dir):
        """A spec kiro-cli WOULD resolve must not read here as "no spec".

        ``<project>/.kiro/agents`` is resolved by kiro-cli BEFORE the user level
        (``config.paths.project_agents_dir``). Resolving only the user level made a
        project-only agent find nothing, so the ``tools`` allowlist never ran and the
        control plane mounted unrestricted -- a restriction the user declared, lost.
        Withholding what the spec does not name is the whole point of the allowlist.
        """
        _write_project_spec(
            tmp_path,
            servers={"proj": {"command": "/bin/proj"}},
            tools=["@proj"],
        )
        names = _by_name(session_mcp.session_mcp_servers("kirocrew", work_dir=tmp_path))
        assert "proj" in names
        # tools names only @proj, and the control plane is not exempt from it.
        assert "kirocrew-core" not in names
        assert "kirocrew-cron" not in names

    def test_a_project_only_agent_with_no_work_dir_still_finds_nothing(self, tmp_path, agents_dir):
        """The plumbing is what makes it resolvable; without it, nothing changed.

        Pinned so a later refactor that drops ``work_dir`` at any call site fails
        here rather than silently reverting to the user-level-only resolution.
        """
        _write_project_spec(
            tmp_path,
            servers={"proj": {"command": "/bin/proj"}},
            tools=["@proj"],
        )
        assert "proj" not in _by_name(session_mcp.session_mcp_servers("kirocrew"))

    def test_the_project_spec_wins_over_a_user_level_one(self, tmp_path, agents_dir):
        """Project-nearest, the way a nearer config layer normally wins."""
        _write_spec(agents_dir, servers={"user": {"command": "/bin/user"}}, tools=["@user"])
        _write_project_spec(
            tmp_path,
            servers={"proj": {"command": "/bin/proj"}},
            tools=["@proj"],
        )
        names = _by_name(session_mcp.session_mcp_servers("kirocrew", work_dir=tmp_path))
        assert "proj" in names
        assert "user" not in names

    def test_a_project_only_agents_disabled_tools_reach_the_deny_rules(self, tmp_path, agents_dir):
        """``disabledTools`` is a RESTRICTION, so the same resolution gap dropped it."""
        _write_project_spec(
            tmp_path,
            servers={"proj": {"command": "/bin/proj", "disabledTools": ["danger"]}},
            tools=["@proj"],
        )
        assert session_mcp.session_mcp_deny_rules("kirocrew", work_dir=tmp_path) == [
            "mcp__proj__danger"
        ]
        # And without the checkout it is silently lost -- the defect, pinned.
        assert session_mcp.session_mcp_deny_rules("kirocrew") == []

    def test_a_stubbed_server_yields_to_its_broker_stub(self, agents_dir):
        """The caller appends the stub under the SAME name; two would collide.

        Either the raw entry shadows the stub and the session bypasses the broker,
        or both register and every pooled backend runs twice (#927).
        """
        _write_spec(
            agents_dir,
            servers={"pooled": {"command": "/bin/raw"}, "direct": {"command": "/bin/direct"}},
            tools=["@pooled", "@direct"],
        )
        names = _by_name(
            session_mcp.session_mcp_servers("kirocrew", stub_server_names=frozenset({"pooled"}))
        )
        assert "pooled" not in names
        assert "direct" in names

    def test_a_stubbed_control_plane_server_also_yields(self, agents_dir):
        # The control plane is re-derived AFTER the registry filter, so the stub
        # drop has to run after that re-add or a pooled kirocrew-core comes back.
        names = _by_name(
            session_mcp.session_mcp_servers(
                "kirocrew", stub_server_names=frozenset({"kirocrew-core"})
            )
        )
        assert "kirocrew-core" not in names
        assert "kirocrew-cron" in names

    def test_registry_type_matches_the_spec_writer(self):
        # A rename in agent.py must not silently stop this filter from matching.
        assert session_mcp._KIRO_REGISTRY_TYPE == agent_mod._MCP_REGISTRY_TYPE


class TestDenyRules:
    def test_disabled_tools_become_deny_rules(self, agents_dir):
        # disabledTools is a RESTRICTION: dropping it while forwarding the server
        # it narrows would widen the session's tool surface behind the user's back.
        _write_spec(
            agents_dir,
            servers={"srv": {"command": "/bin/srv", "disabledTools": ["danger", "worse"]}},
            tools=["@srv"],
        )
        assert session_mcp.session_mcp_deny_rules("kirocrew") == [
            "mcp__srv__danger",
            "mcp__srv__worse",
        ]

    def test_no_disabled_tools_means_no_rules(self, agents_dir):
        _write_spec(agents_dir, servers={"srv": {"command": "/bin/srv"}}, tools=["@srv"])
        assert session_mcp.session_mcp_deny_rules("kirocrew") == []

    def test_malformed_spec_yields_no_rules(self, agents_dir):
        (agents_dir / "kirocrew.json").write_text("{not json", encoding="utf-8")
        assert session_mcp.session_mcp_deny_rules("kirocrew") == []
        assert session_mcp.session_mcp_deny_rules(None) == []


class TestControlPlane:
    def test_loaded_when_no_spec_exists(self, agents_dir):
        names = _by_name(session_mcp.session_mcp_servers("kirocrew"))
        assert set(names) == {"kirocrew-core", "kirocrew-cron"}
        assert names["kirocrew-core"]["args"] == ["mcp-core"]

    def test_loaded_when_the_spec_is_malformed(self, agents_dir):
        (agents_dir / "kirocrew.json").write_text("{not json", encoding="utf-8")
        assert set(_by_name(session_mcp.session_mcp_servers("kirocrew"))) == {
            "kirocrew-core",
            "kirocrew-cron",
        }

    def test_stale_spec_command_is_refreshed_from_the_managed_source(self, agents_dir):
        _write_spec(
            agents_dir,
            servers={"kirocrew-core": {"command": "/gone/kirocrew", "args": ["mcp-core"]}},
            tools=["@kirocrew-core"],
        )
        core = _by_name(session_mcp.session_mcp_servers("kirocrew"))["kirocrew-core"]
        assert core["command"] == "/opt/kirocrew"

    def test_a_spec_that_drops_the_reference_still_drops_the_server(self, agents_dir):
        # The refresh must not become a re-grant: kiro-cli would not mount a
        # server its tools list does not name, and neither may claude.
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        assert "kirocrew-core" not in _by_name(session_mcp.session_mcp_servers("kirocrew"))

    def test_no_agent_means_control_plane_only(self, agents_dir):
        assert set(_by_name(session_mcp.session_mcp_servers(None))) == {
            "kirocrew-core",
            "kirocrew-cron",
        }


class TestClientSeam:
    def _seeded(self, tmp_path, **kw):
        """A client whose settings seed has already run, as the spawn path does.

        The array is withheld unless Crew authored ``settings.local.json`` -- that
        file is the backend's permission surface, and a tool Crew cannot gate is
        not handed over at all. So a seam test that skips the seed exercises the
        withhold path, not the translation. The spawn path runs the writer first
        for the same reason; see ``_resolve_session_mcp_servers``.
        """
        client = AcpClient(work_dir=tmp_path, **kw)
        client._write_claude_local_settings()
        assert client._claude_settings_authored is True
        return client

    def test_kiro_backend_passes_no_array(self, tmp_path, agents_dir):
        client = AcpClient(work_dir=tmp_path)
        # kiro-cli receives the same servers via --agent; a duplicate here would
        # shadow the spec's own entries.
        assert client._session_mcp_servers() == []

    def test_claude_backend_translates_the_spec(self, tmp_path, agents_dir):
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        assert "foo" in _by_name(client._session_mcp_servers())

    def test_neither_gate_is_an_identity_check(self, tmp_path, agents_dir, monkeypatch):
        """Two gates decide the seam, and neither reads the harness's identity.

        The capability set decides WHETHER the array is consulted (a property of the
        transport, not the vendor -- harness-parity H6), and the mirror registry
        decides WHAT fills it. Widening the set alone must NOT populate: a backend
        with no registered mirror has nothing to contribute and fails closed. Add
        both and it works with no edit at either call site, which is what proves no
        identity branch has crept back in.
        """
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(tmp_path, agent="kirocrew")
        assert client._session_mcp_servers() == []

        # Set widened, no mirror registered -> still nothing. Fail-closed.
        monkeypatch.setattr(
            client_mod, "ACP_BACKENDS_SESSION_MCP_ARRAY", frozenset({client.backend})
        )
        client._reset_state()
        client._write_claude_local_settings()  # reset ends the session's ownership
        assert client._session_mcp_servers() == []

        # Register a mirror for that backend too, and the array populates.
        monkeypatch.setitem(mirrors_registry.MIRRORS, client.backend, ClaudeCodeMirror)
        client._reset_state()
        client._write_claude_local_settings()
        assert "foo" in _by_name(client._session_mcp_servers())

    def test_the_seam_hands_down_the_pooled_stub_names(self, tmp_path, agents_dir, monkeypatch):
        # The client owns the overlay, so it is the only layer that can answer
        # which servers will ALSO arrive as broker stubs.
        _write_spec(
            agents_dir,
            servers={"pooled": {"command": "/bin/raw"}, "direct": {"command": "/bin/direct"}},
            tools=["@pooled", "@direct"],
        )
        monkeypatch.setattr(
            client_mod, "injection_server_names", lambda _o, _a: frozenset({"pooled"})
        )
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        names = _by_name(client._session_mcp_servers())
        assert "pooled" not in names
        assert "direct" in names

    def test_an_unreadable_overlay_does_not_cost_the_session_its_servers(
        self, tmp_path, agents_dir, monkeypatch
    ):
        # Empty is the safe direction: re-declaring a stubbed server lets the
        # injection outrank it, while withholding one nothing else supplies is a
        # session with missing tools.
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])

        def _boom(_o, _a):
            raise RuntimeError("overlay unreadable")

        monkeypatch.setattr(client_mod, "injection_server_names", _boom)
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        assert "foo" in _by_name(client._session_mcp_servers())

    def test_the_shared_call_site_reads_no_disk_for_kiro(self, tmp_path, agents_dir, monkeypatch):
        """harness-parity H13: the kiro construction path gains nothing.

        Both session-params call sites are shared with kiro-cli, so the accessor
        they call must be synchronous AND must not reach the translator for a
        backend outside the capability set. If it did, adapter work would have put
        a new scheduling and failure point on kiro's ``session/new``.
        """

        def _never(*_a, **_kw):
            raise AssertionError("the kiro path must not translate a spec")

        monkeypatch.setattr(claude_mirror, "session_mcp_servers", _never)
        monkeypatch.setattr(client_mod, "injection_server_names", _never)
        client = AcpClient(work_dir=tmp_path, agent="kirocrew")
        result = client._session_mcp_servers()
        assert result == []
        # A coroutine here would force the shared call site to await.
        assert not hasattr(result, "__await__")

    def test_the_array_is_resolved_once_and_dropped_on_reset(
        self, tmp_path, agents_dir, monkeypatch
    ):
        """Cached per spawn, not per call site, and re-read on the next spawn.

        session/new and the session/load that resumes it both read the accessor;
        translating twice would double the disk work for one session. Clearing on
        reset is what keeps the "installing a server takes effect on the NEXT
        session" promise.
        """
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        calls: list[int] = []
        real = session_mcp.session_mcp_servers

        def _counted(*a, **kw):
            calls.append(1)
            return real(*a, **kw)

        monkeypatch.setattr(claude_mirror, "session_mcp_servers", _counted)
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        assert "foo" in _by_name(client._session_mcp_servers())
        assert "foo" in _by_name(client._session_mcp_servers())
        assert len(calls) == 1
        client._reset_state()
        assert client._session_mcp_cache is None
        client._write_claude_local_settings()  # the next spawn re-seeds before resolving
        assert "foo" in _by_name(client._session_mcp_servers())
        assert len(calls) == 2

    def test_no_tools_reach_a_session_whose_permissions_crew_does_not_own(
        self, tmp_path, agents_dir
    ):
        """Tools are delivered only where Crew can still withhold their use.

        Crew's gate fires on ``session/request_permission``. A tool pre-approved in
        ``permissions.allow`` never sends one, so Crew sees the ``tool_call``
        notification after the fact and cannot stop it. The seed Crew authors is
        what puts a session under the gate -- and the writer is create-or-decline,
        so a project carrying its OWN settings.local.json gets no seed and Crew
        governs nothing there. Handing that session the array would deliver
        spawn_run, cron_add, send_message and every configured server into a
        permission surface Crew does not control.
        """
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        path = tmp_path / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"permissions": {"allow": ["mcp__foo__write"]}}), encoding="utf-8"
        )

        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._write_claude_local_settings()  # declines: the file is not Crew's
        assert client._claude_settings_authored is False

        # No partial delivery -- the whole array is withheld, which is exactly how
        # a claude session behaved before this array existed. Nothing regresses;
        # it simply does not gain a tool Crew could not take back.
        assert client._session_mcp_servers() == []
        assert client._write_claude_local_settings() is None
        assert path.read_text() == json.dumps({"permissions": {"allow": ["mcp__foo__write"]}})

    def test_the_cached_array_is_not_aliased_to_callers(self, tmp_path, agents_dir):
        # The two call sites splat this list into their params; handing out the
        # cache itself would let one session/load mutation reach the next.
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        first = client._session_mcp_servers()
        first.clear()
        assert "foo" in _by_name(client._session_mcp_servers())


class TestLocalSettingsSeed:
    """Crew's seed of ``<work_dir>/.claude/settings.local.json``.

    The governing rule is ownership, and ownership is NOT the path -- a path under
    a checked-out repository is not Crew's to claim. It is having CREATED the file
    in this session AND the bytes on disk still being the ones Crew wrote. Both
    hold: Crew may overwrite (which is what lets a model-substitution re-seed
    change the resolved model) and reset removes it. Either fails: Crew leaves the
    path entirely alone, and reset removes nothing. Absent: Crew creates it with
    ``O_EXCL``.

    Nothing here reads, merges into, rewrites or deletes a file Crew did not
    author, which is what keeps a seam that writes into a checked-out project from
    needing a snapshot, a cross-session ownership registry, or a restore write on
    teardown.
    """

    def _client(self, tmp_path, **kw):
        return AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE, **kw)

    def test_seed_writes_the_model_allowlist(self, tmp_path):
        from kiro_crew import model_registry

        client = self._client(tmp_path)
        client._write_claude_local_settings()
        data = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
        # Without the allowlist the adapter can collapse a versioned [1m] id back
        # to the 200K window. The seed writes the window-deduped list (a 200K base
        # id is dropped when its 1M sibling is present), so it can differ from the
        # raw registry list — compare against seed_available_models, the deduped
        # source the seed actually uses.
        assert data["availableModels"] == model_registry.seed_available_models("claude_code")

    def test_no_permission_mode_leaves_the_adapter_default(self, tmp_path):
        client = self._client(tmp_path)
        client._write_claude_local_settings()
        data = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
        assert "permissions" not in data

    def test_permission_mode_is_written_when_requested(self, tmp_path):
        client = self._client(tmp_path, permission_mode="default")
        client._write_claude_local_settings()
        data = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
        assert data["permissions"]["defaultMode"] == "default"

    def test_resolved_model_written_but_auto_omitted(self, tmp_path):
        auto = self._client(tmp_path)
        auto._write_claude_local_settings()
        path = tmp_path / ".claude" / "settings.local.json"
        assert "model" not in json.loads(path.read_text())
        # A second client only writes when the path is free, so clear the first
        # session's file the way its own reset would.
        auto._claude_settings_authored = False
        path.unlink()
        pinned = self._client(tmp_path, model="claude-sonnet-4-5")
        pinned._write_claude_local_settings()
        assert json.loads(path.read_text())["model"] == "claude-sonnet-4-5"

    def test_disabled_tools_reach_the_settings_deny_list(self, tmp_path, agents_dir):
        _write_spec(
            agents_dir,
            servers={"srv": {"command": "/bin/srv", "disabledTools": ["danger"]}},
            tools=["@srv"],
        )
        client = self._client(tmp_path, agent="kirocrew")
        client._write_claude_local_settings()
        data = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
        # disabledTools cannot ride along in the mcpServers array, so dropping it
        # while still forwarding the server would widen the tool surface.
        assert "mcp__srv__danger" in data["permissions"]["deny"]

    def test_a_file_crew_created_is_removed_on_reset(self, tmp_path):
        client = self._client(tmp_path)
        client._write_claude_local_settings()
        path = tmp_path / ".claude" / "settings.local.json"
        assert path.exists()
        client._reset_state()
        # A permission mode must not outlive its session, and an inherited
        # bypassPermissions must not survive a crash.
        assert not path.exists()

    def test_an_existing_file_is_never_read_written_or_removed(self, tmp_path):
        """The whole ownership rule, in one test.

        A pre-existing file belongs to the user (or to a live sibling session
        sharing this ``work_dir``). Crew authored neither, so it seeds nothing and
        its reset removes nothing -- there is no merge to get wrong, no snapshot
        to arbitrate, and no restore write to perform.
        """
        path = tmp_path / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True)
        original = json.dumps({"permissions": {"allow": ["Bash(ls)"]}, "env": {"X": "1"}}, indent=2)
        path.write_text(original, encoding="utf-8")

        client = self._client(tmp_path, permission_mode="default")
        client._write_claude_local_settings()
        assert path.read_text() == original
        assert client._claude_settings_authored is False

        client._reset_state()
        assert path.read_text() == original

    def test_an_inherited_bypass_mode_is_left_to_its_owner(self, tmp_path):
        """The disclosed cost of not touching a file Crew did not author.

        ``bypassPermissions`` takes every tool call out of the host gate, and Crew
        no longer strips it -- stripping meant reading and rewriting a path a
        checked-out repository controls, which is what produced the snapshot and
        restore machinery. The call still reaches Crew's gate unless the user's
        own file pre-approves it, the same boundary the inherited-``~/.claude``
        gap already documents.
        """
        path = tmp_path / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True)
        original = json.dumps({"permissions": {"defaultMode": "bypassPermissions"}}, indent=2)
        path.write_text(original, encoding="utf-8")

        client = self._client(tmp_path)
        client._write_claude_local_settings()
        assert path.read_text() == original

    def test_a_symlinked_settings_file_is_refused(self, tmp_path):
        """A dangling link is absent to exists(), so the CREATE is the exposure."""
        target = tmp_path / "secret.json"
        path = tmp_path / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True)
        path.symlink_to(target)

        client = self._client(tmp_path)
        client._write_claude_local_settings()
        # Neither the link nor its target is written.
        assert not target.exists()
        assert client._claude_settings_authored is False

    def test_a_symlinked_claude_directory_is_refused_too(self, tmp_path):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (tmp_path / ".claude").symlink_to(elsewhere, target_is_directory=True)

        client = self._client(tmp_path)
        client._write_claude_local_settings()
        assert not (elsewhere / "settings.local.json").exists()

    def test_a_sensitive_resolved_target_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(client_mod, "is_sensitive_path", lambda _p: True)
        c = self._client(tmp_path)
        c._write_claude_local_settings()
        assert not (tmp_path / ".claude" / "settings.local.json").exists()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
    def test_the_file_crew_creates_is_owner_only(self, tmp_path):
        """The seed can carry a permission mode, so it must not be world-readable.

        POSIX only: Windows maps the ``os.open`` mode argument onto the read-only
        attribute alone, so a writable file always reads back as 0o666 there and
        the assertion says nothing about either platform.
        """
        client = self._client(tmp_path)
        client._write_claude_local_settings()
        path = tmp_path / ".claude" / "settings.local.json"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_a_concurrent_create_is_not_clobbered(self, tmp_path):
        """O_EXCL is the real ownership claim; exists() is only the fast path.

        Two sessions sharing one ``work_dir`` can both pass the exists() check,
        so the create itself has to lose the race rather than overwrite.
        """
        path = tmp_path / ".claude" / "settings.local.json"
        client = self._client(tmp_path)
        real_open = os.open

        def racing_open(p, flags, *a, **kw):
            if str(p) == str(path) and not path.exists():
                path.write_text('{"env": {"sibling": "1"}}', encoding="utf-8")
            return real_open(p, flags, *a, **kw)

        with mock.patch.object(client_mod.os, "open", side_effect=racing_open):
            client._write_claude_local_settings()

        assert json.loads(path.read_text()) == {"env": {"sibling": "1"}}
        assert client._claude_settings_authored is False
        client._reset_state()
        assert path.exists()

    def test_seed_failure_does_not_break_the_spawn_path(self, tmp_path, monkeypatch):
        """The seed is best-effort: losing it must not cost the whole session."""
        client = self._client(tmp_path)

        def boom(*_a, **_kw):
            raise OSError("disk full")

        monkeypatch.setattr(client_mod.os, "open", boom)
        with pytest.raises(OSError):
            client._write_claude_local_settings()
        # Nothing was authored, so reset has nothing to undo.
        assert client._claude_settings_authored is False
        client._reset_state()

    def test_reset_without_a_seed_removes_nothing(self, tmp_path):
        path = tmp_path / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True)
        path.write_text("{}", encoding="utf-8")
        client = self._client(tmp_path)
        client._reset_state()
        assert path.exists()

    def test_a_reseed_overwrites_the_file_this_session_created(self, tmp_path):
        """The model-substitution retry has to be able to change what it wrote.

        ``_new_session_following_substitution`` adopts the gateway-served model and
        re-seeds so the fresh ``SettingsManager`` the adapter builds for the retry
        resolves it. Declining here -- because the path now holds a file -- would
        send byte-identical ``session/new`` params, take the same substitution
        advisory, and fail the session with "even after adopting substitute model".
        """
        client = self._client(tmp_path)
        client._write_claude_local_settings()
        path = tmp_path / ".claude" / "settings.local.json"
        assert "model" not in json.loads(path.read_text())

        client._model = "claude-sonnet-4-5"
        client._write_claude_local_settings()

        assert json.loads(path.read_text())["model"] == "claude-sonnet-4-5"
        assert client._claude_settings_authored is True
        # And it is still Crew's to remove.
        client._reset_state()
        assert not path.exists()

    def test_a_reseed_leaves_a_file_the_user_replaced_after_the_create(self, tmp_path):
        """Creating the file is not ownership on its own.

        A user can replace it atomically (write-temp + rename) between the create
        and the re-seed. The replacement is theirs: the flag alone would let the
        re-seed clobber it, so the bytes are compared too.
        """
        client = self._client(tmp_path)
        client._write_claude_local_settings()
        path = tmp_path / ".claude" / "settings.local.json"

        mine = json.dumps({"permissions": {"allow": ["Bash(ls)"]}}, indent=2)
        path.write_text(mine, encoding="utf-8")

        client._model = "claude-sonnet-4-5"
        client._write_claude_local_settings()

        assert path.read_text() == mine
        # The claim is dropped, so reset does not delete it either.
        assert client._claude_settings_authored is False
        client._reset_state()
        assert path.read_text() == mine

    def test_reset_leaves_a_file_the_user_replaced_after_the_create(self, tmp_path):
        """Same rule on the teardown path, which is where a delete would land."""
        client = self._client(tmp_path)
        client._write_claude_local_settings()
        path = tmp_path / ".claude" / "settings.local.json"

        mine = json.dumps({"env": {"MINE": "1"}}, indent=2)
        path.write_text(mine, encoding="utf-8")

        client._reset_state()

        assert path.read_text() == mine
        assert client._claude_settings_authored is False

    def test_a_file_removed_under_crew_is_created_again(self, tmp_path):
        """The path is free again, so the re-seed creates rather than claims."""
        client = self._client(tmp_path)
        client._write_claude_local_settings()
        path = tmp_path / ".claude" / "settings.local.json"
        path.unlink()

        client._write_claude_local_settings()

        assert path.exists()
        assert client._claude_settings_authored is True

    def test_the_bytes_on_disk_are_exactly_the_bytes_recorded(self, tmp_path):
        """Ownership is byte equality, so the write must not translate anything.

        The seed was first written in TEXT mode, and Python's text layer rewrites
        "\n" to "\r\n" on Windows -- so the file on disk was longer than the payload
        the session recorded, byte equality never held, and every Windows session
        read as "not ours": the re-seed declined, reset never removed its own file,
        and the MCP array was withheld. Nothing on POSIX could see it, which is why
        the invariant is asserted here rather than left to the platform.
        """
        client = self._client(tmp_path, permission_mode="default")
        client._write_claude_local_settings()
        path = tmp_path / ".claude" / "settings.local.json"
        written = client._claude_settings_written
        assert written is not None

        raw = path.read_bytes()
        assert raw == written.encode("utf-8")
        assert path.stat().st_size == len(written.encode("utf-8"))
        assert b"\r\n" not in raw
        # And the ownership check therefore agrees with itself on every platform.
        assert client._claude_settings_is_still_ours() is True

    @pytest.mark.skipif(os.name == "nt", reason="POSIX FIFOs")
    def test_a_fifo_swapped_in_after_the_create_neither_hangs_nor_is_claimed(self, tmp_path):
        """``_reset_state`` is synchronous and runs ON the event loop.

        By teardown the path is whatever the world left there. A plain read of a
        FIFO blocks on the open until someone writes, which would hold the whole
        gateway's loop, not just this session. The check opens with O_NONBLOCK and
        refuses anything that is not a regular file, so the swap answers "not
        ours" -- and a file Crew does not own is left in place.
        """
        client = self._client(tmp_path)
        client._write_claude_local_settings()
        path = tmp_path / ".claude" / "settings.local.json"
        path.unlink()
        os.mkfifo(path)

        # Would block forever on a read_text(); must return promptly.
        assert client._claude_settings_is_still_ours() is False

        client._reset_state()
        assert stat.S_ISFIFO(path.stat(follow_symlinks=False).st_mode)
        assert client._claude_settings_authored is False

    def test_a_huge_replacement_is_refused_without_being_read(self, tmp_path):
        """Size settles it, so a multi-gigabyte file never enters memory.

        The comparison is against a few hundred bytes Crew wrote, so any other
        length is already a mismatch -- checking it first is both cheaper and the
        thing that bounds the read.
        """
        client = self._client(tmp_path)
        client._write_claude_local_settings()
        path = tmp_path / ".claude" / "settings.local.json"
        written = client._claude_settings_written
        assert written is not None

        reads: list[int] = []
        real_read = os.read

        def counting_read(fd, n, *a, **kw):
            reads.append(n)
            return real_read(fd, n, *a, **kw)

        # Sparse, so the test does not actually write a gigabyte.
        with open(path, "r+b") as handle:
            handle.truncate(1 << 30)

        with mock.patch.object(client_mod.os, "read", side_effect=counting_read):
            assert client._claude_settings_is_still_ours() is False
        assert reads == [], "a size mismatch must settle it before any read"

        client._reset_state()
        assert path.exists()

    def test_the_read_is_capped_at_the_payload_it_compares(self, tmp_path):
        """Even a same-size file is read bounded, never whole-file."""
        client = self._client(tmp_path)
        client._write_claude_local_settings()
        written = client._claude_settings_written
        assert written is not None

        sizes: list[int] = []
        real_read = os.read

        def counting_read(fd, n, *a, **kw):
            sizes.append(n)
            return real_read(fd, n, *a, **kw)

        with mock.patch.object(client_mod.os, "read", side_effect=counting_read):
            assert client._claude_settings_is_still_ours() is True
        assert sizes == [len(written.encode("utf-8")) + 1]

    def test_reset_reads_the_ownership_flag_defensively(self, tmp_path):
        """``_reset_state`` runs on clients built without ``__init__``.

        Several suites construct an ``AcpClient`` via ``__new__`` and set only the
        fields the unit under test needs (``test_acp_usage_cost``'s bare client is
        the one that caught this). Reading the ownership flag unconditionally
        raised ``AttributeError`` there, on a path shared with every real session,
        so the read is a ``getattr`` with the safe default -- absent means "Crew
        authored nothing", which removes nothing.
        """
        client = self._client(tmp_path)
        del client._claude_settings_authored
        client._reset_state()  # must not raise
