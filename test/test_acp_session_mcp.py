"""Session-array MCP wiring: agent spec -> session/new mcpServers array.

A backend in ``ACP_BACKENDS_SESSION_MCP_ARRAY`` (claude-agent-acp today) receives
its MCP servers ONLY through the ``session/new`` / ``session/load`` parameter, so
these tests pin the shape the adapter's schema requires (``env``/``headers`` always arrays, an explicit transport ``type``) and
the mounting rules the kiro agent spec expresses (``tools`` references, the
registry pointer, Crew's own control plane).

The client seam (what ``AcpClient`` puts on ``session/new``) is in
``test_acp_session_mcp_client_seam.py``, and the fixtures and spec writers both
modules use in ``acp_session_mcp_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path
from unittest import mock

import pytest
from acp_session_mcp_helpers import _installed_claude_adapter_version  # noqa: F401
from acp_session_mcp_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_session_mcp_helpers import agents_dir_fixture  # noqa: F401
from acp_session_mcp_helpers import isolated_seed_provenance  # noqa: F401
from acp_session_mcp_helpers import _by_name, _write_project_spec, _write_spec

from kiro_crew import agent as agent_mod
from kiro_crew.acp import client as client_mod
from kiro_crew.acp import session_mcp
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE
from kiro_crew.providers.mirrors import claude_code as claude_mirror
from kiro_crew.providers.mirrors.claude_code import ClaudeCodeMirror


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
    def test_dotted_template_keeps_its_mcp_allowlist(self, agents_dir):
        _write_spec(
            agents_dir,
            name="reviewer.v2",
            servers={"granted": {"command": "/bin/a"}, "ungranted": {"command": "/bin/b"}},
            tools=["@granted"],
        )
        assert set(_by_name(session_mcp.session_mcp_servers("reviewer.v2"))) == {"granted"}

    def test_server_not_referenced_by_tools_is_withheld(self, agents_dir):
        _write_spec(
            agents_dir,
            servers={"granted": {"command": "/bin/a"}, "ungranted": {"command": "/bin/b"}},
            tools=["@granted"],
        )
        names = _by_name(session_mcp.session_mcp_servers("kirocrew"))
        assert "granted" in names
        assert "ungranted" not in names

    @pytest.mark.parametrize(
        "tools",
        [
            pytest.param(["@foo/only_this"], id="tool_scoped_reference_mounts_the_server"),
            pytest.param(["*"], id="wildcard_reference_mounts_everything"),
        ],
    )
    def test_reference_mounts_the_server(self, agents_dir, tools):
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=tools)
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

        The checkout's own ``mcpServers`` never reach a mirrored session (see
        ``_project_mcp_trusted``), so ``proj`` is withheld too -- but the allowlist
        still governs what is left, which is the restriction this pins.
        """
        _write_project_spec(
            tmp_path,
            servers={"proj": {"command": "/bin/proj"}},
            tools=["@proj"],
        )
        names = _by_name(session_mcp.session_mcp_servers("kirocrew", work_dir=tmp_path))
        assert "proj" not in names
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

    def test_the_user_level_spec_wins_over_an_untrusted_project_one(self, tmp_path, agents_dir):
        """A checkout cannot choose a mirrored session's servers, nor displace the user's.

        The plain resolution is still project-nearest -- the unresolved-ref
        diagnostic, which runs on kiro-cli's path too, reads the project spec.
        """
        _write_spec(agents_dir, servers={"user": {"command": "/bin/user"}}, tools=["@user"])
        _write_project_spec(
            tmp_path,
            servers={"proj": {"command": "/bin/proj"}},
            tools=["@proj"],
        )
        names = _by_name(session_mcp.session_mcp_servers("kirocrew", work_dir=tmp_path))
        assert "proj" not in names
        assert "user" in names
        snapshot = session_mcp.agent_spec_snapshot("kirocrew", work_dir=tmp_path)
        assert snapshot is not None
        assert "proj" in snapshot["mcpServers"]

    def test_deny_rules_follow_the_same_resolution_as_the_array(self, tmp_path, agents_dir):
        """Claude's deny rules and the array read ONE answer for a checkout's spec.

        The project's servers never launch, but its ``disabledTools`` are a
        restriction and survive; a user-level spec of the same name adds its own.
        """
        _write_project_spec(
            tmp_path,
            servers={"proj": {"command": "/bin/proj", "disabledTools": ["danger"]}},
            tools=["@proj"],
        )
        assert session_mcp.session_mcp_deny_rules("kirocrew", work_dir=tmp_path) == [
            "mcp__proj__danger"
        ]
        _write_spec(
            agents_dir,
            servers={"user": {"command": "/bin/user", "disabledTools": ["risky"]}},
            tools=["@user"],
        )
        assert session_mcp.session_mcp_deny_rules("kirocrew", work_dir=tmp_path) == [
            "mcp__proj__danger",
            "mcp__user__risky",
        ]

    def test_disabled_tools_is_the_structured_form_and_exempts_no_server(self, agents_dir):
        """``(server, tool)`` pairs, the control plane included.

        The deny-rule spelling is lossy (``mcp__a__b__c`` splits on the LAST
        ``__``), so a consumer comparing against a two-field identity must take the
        pairs. And this set answers "what did the user switch off", which is as true
        of ``kirocrew-core`` as of any server -- HOW a backend honours it is the
        caller's question (the restricted set exempts the control plane from
        withholding; this does not exempt it from anything).
        """
        _write_spec(
            agents_dir,
            servers={
                "kirocrew-core": {"command": "/x", "disabledTools": ["spawn_run"]},
                "third": {"command": "/y", "disabledTools": ["a__b", 3, ""]},
                "clean": {"command": "/z"},
            },
            tools=["@kirocrew-core", "@third", "@clean"],
        )
        pairs = session_mcp.session_mcp_disabled_tools("kirocrew")
        assert pairs == frozenset({("kirocrew-core", "spawn_run"), ("third", "a__b")})
        # The rule spelling is derived from the same pairs, so the two cannot drift.
        assert session_mcp.session_mcp_deny_rules("kirocrew") == [
            "mcp__kirocrew-core__spawn_run",
            "mcp__third__a__b",
        ]
        assert session_mcp.session_mcp_disabled_tools(None) == frozenset()

    def test_disabled_tools_written_by_the_dashboard_to_the_global_file_count(
        self, tmp_path, agents_dir
    ):
        """The dashboard's tool-off action writes ``disabledTools`` to the GLOBAL
        ``settings/mcp.json`` and nowhere else, and the spec rebuild never copies a
        global entry onto a managed server -- so a restriction on ``kirocrew-core``
        written the ordinary way lives only there. kiro-cli reads both; so must this,
        or the one path a user actually takes is the one that is missed."""
        _write_spec(
            agents_dir,
            servers={"kirocrew-core": {"command": "/x"}, "third": {"command": "/y"}},
            tools=["@kirocrew-core", "@third"],
        )
        (tmp_path / "settings-mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "kirocrew-core": {"disabledTools": ["spawn_run"]},
                        "third": {"command": "/y", "disabledTools": ["a"]},
                        "elsewhere": {"disabledTools": ["b"]},
                    }
                }
            ),
            encoding="utf-8",
        )
        pairs = session_mcp.session_mcp_disabled_tools("kirocrew")
        assert {("kirocrew-core", "spawn_run"), ("third", "a"), ("elsewhere", "b")} <= pairs
        # The withhold set is derived from the SAME pairs: a third-party server the
        # dashboard narrowed globally is withheld, the control plane never is, and a
        # server named only in the global file is a harmless name nothing mounts.
        restricted = session_mcp.session_mcp_projection("kirocrew").restricted
        assert "third" in restricted
        assert "kirocrew-core" not in restricted
        assert "elsewhere" in restricted
        # Both sources reach claude's deny rules too.
        assert "mcp__kirocrew-core__spawn_run" in session_mcp.session_mcp_deny_rules("kirocrew")
        # A malformed or missing global file switches nothing off from that source.
        (tmp_path / "settings-mcp.json").write_text("[not a dict]", encoding="utf-8")
        assert session_mcp.session_mcp_disabled_tools("kirocrew") == frozenset()

    def test_an_explicit_none_spec_is_honoured_not_re_read(self, agents_dir, monkeypatch):
        """``None`` means "there is no spec"; only "not supplied" reads.

        The projection reads once and threads the ANSWER. If ``None`` also meant
        "read it", a read that came back empty would have every helper read again,
        and a spec appearing in between would be translated by one helper while
        the allowlist -- computed from the ``None`` -- granted everything.
        """
        _write_spec(
            agents_dir, servers={"srv": {"command": "/s", "disabledTools": ["t"]}}, tools=["@srv"]
        )
        reads: list[str] = []
        real = session_mcp._agent_spec_and_snapshot_for

        def counting(agent, work_dir=None, **kwargs):
            reads.append(agent)
            return real(agent, work_dir, **kwargs)

        monkeypatch.setattr(session_mcp, "_agent_spec_and_snapshot_for", counting)
        # Explicit None: no spec, nothing read, control plane only, nothing switched off.
        names = [e["name"] for e in session_mcp.session_mcp_servers("kirocrew", spec=None)]
        assert names == ["kirocrew-core", "kirocrew-cron"]
        assert session_mcp.session_mcp_restricted_servers(frozenset()) == frozenset()
        assert session_mcp.session_mcp_disabled_tools("kirocrew", spec=None) == frozenset()
        assert reads == []
        # Not supplied: read, once per call, and the spec's own answer applies.
        assert "srv" in [e["name"] for e in session_mcp.session_mcp_servers("kirocrew")]
        assert reads == ["kirocrew"]

    def test_the_projection_threads_the_answer_even_when_it_is_no_spec(
        self, agents_dir, monkeypatch
    ):
        """The window itself: the first read finds nothing, a spec appears, and no
        helper may see it. Every part of the projection reflects the SAME read."""
        spec_path = agents_dir / "kirocrew.json"
        _write_spec(agents_dir, servers={"late": {"command": "/l"}}, tools=["@late"])
        real = session_mcp._agent_spec_and_snapshot_for
        calls = {"n": 0}

        def flapping(agent, work_dir=None, **kwargs):
            calls["n"] += 1
            return (None, None) if calls["n"] == 1 else real(agent, work_dir, **kwargs)

        monkeypatch.setattr(session_mcp, "_agent_spec_and_snapshot_for", flapping)
        projection = session_mcp.session_mcp_projection("kirocrew")
        assert calls["n"] == 1, "a helper read the spec behind the projection's back"
        assert "late" not in [e["name"] for e in projection.servers]
        assert projection.allowlist.applies is False
        assert spec_path.exists()

    def test_a_stubbed_server_yields_to_its_broker_stub(self, agents_dir):
        """The caller appends the stub under the SAME name; two would collide.

        Either the raw entry shadows the stub and the session bypasses the broker,
        or both register and every pooled backend runs twice.
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


class TestZeroTools:
    """``SessionMcpProjection.zero_tools`` -- an explicit empty ``tools`` list,
    not a missing key, which means "no restriction stated" and must NOT trip
    ``AcpClient._deny_zero_tools``."""

    def test_an_explicit_empty_list_is_zero_tools(self, agents_dir):
        _write_spec(agents_dir, servers={}, tools=[])
        assert session_mcp.session_mcp_projection("kirocrew").zero_tools is True

    def test_a_missing_tools_key_is_not_zero_tools(self, agents_dir):
        _write_spec(agents_dir, servers={}, tools=None)
        assert session_mcp.session_mcp_projection("kirocrew").zero_tools is False

    def test_a_populated_tools_list_is_not_zero_tools(self, agents_dir):
        _write_spec(agents_dir, servers={"srv": {"command": "/s"}}, tools=["@srv"])
        assert session_mcp.session_mcp_projection("kirocrew").zero_tools is False

    def test_no_agent_is_not_zero_tools(self):
        assert session_mcp.session_mcp_projection(None).zero_tools is False

    def test_a_non_list_tools_value_is_not_zero_tools(self, agents_dir):
        (agents_dir / "kirocrew.json").write_text(
            json.dumps({"name": "kirocrew", "tools": "none"}), encoding="utf-8"
        )
        assert session_mcp.session_mcp_projection("kirocrew").zero_tools is False


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


class TestWithholdWarningNamesTheCause:
    """The withhold warning gives no wrong fix, and each cause is logged once.

    Removing the file is not always the fix: an old adapter needs an upgrade, a
    file with hooks needs them taken out, and a sibling session's seed comes
    straight back. So the mirror's line names no single remedy and points at the
    client's own line, which is where each cause and its fix are written.
    """

    _STALE_REMEDY = "Removing or renaming the project's .claude/settings.local.json"

    @staticmethod
    def _withhold_lines(caplog):
        return [
            r.getMessage()
            for r in caplog.records
            if r.name == claude_mirror.__name__ and "withholding the whole" in r.getMessage()
        ]

    def test_the_line_names_no_stale_remedy(self, agents_dir, caplog):
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        with caplog.at_level("WARNING", logger=claude_mirror.__name__):
            projection = ClaudeCodeMirror().session_projection(
                "kirocrew", permission_surface_owned=False
            )
        assert projection.params == {"mcpServers": []}
        [line] = self._withhold_lines(caplog)
        assert self._STALE_REMEDY not in line
        assert "a permissions.allow entry there" not in line
        assert "log lines for that file name the cause" in line

    @pytest.mark.parametrize("cause", ["old-adapter", "permission-mode", "hooks", "link"])
    def test_each_client_refusal_logs_its_cause_at_warning(
        self, tmp_path, agents_dir, caplog, monkeypatch, cause
    ):
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        local = tmp_path / ".claude" / "settings.local.json"
        local.parent.mkdir(parents=True)
        local.write_text(json.dumps({"permissions": {}}), encoding="utf-8")
        kw: dict = {}
        if cause == "link":
            # The usability check is what reads a link; stand it in so the case
            # runs on every host, including one that cannot create symlinks.
            monkeypatch.setattr(client_mod, "_claude_settings_usable", lambda _p: False)
        if cause == "hooks":
            hook = {"type": "command", "command": "./block.sh"}
            (local.parent / "settings.json").write_text(
                json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [hook]}]}}),
                encoding="utf-8",
            )
        if cause == "permission-mode":
            kw["permission_mode"] = "auto"
        client = AcpClient(
            work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE, **kw
        )
        if cause == "old-adapter":
            client._claude_adapter_disk_version = "0.83.0"
        with caplog.at_level("WARNING"):
            client._write_claude_local_settings()
            assert client._session_mcp_servers() == []
        client_lines = [
            r.getMessage()
            for r in caplog.records
            if r.name == client_mod.__name__ and r.levelname == "WARNING"
        ]
        expected = {
            "old-adapter": "Upgrade with 'npm i -g ",
            "permission-mode": "requested permission mode 'auto'",
            "hooks": "sets hooks or sandbox settings",
            "link": "is a symlink or resolves to a sensitive path",
        }[cause]
        assert any(expected in line for line in client_lines)
        [line] = self._withhold_lines(caplog)
        assert self._STALE_REMEDY not in line


class TestPooledStubsOnTheClaudeMirror:
    """The claude mirror places the pooled broker stubs itself (codex parity).

    One withhold rule must cover both halves of the array: a stub carries the
    SAME name as the agent-spec entry it rewrites, so a stub appended by the
    client after the mirror's rules ran would re-add -- unfiltered -- exactly what
    those rules withheld.
    """

    def test_the_mirror_places_the_pooled_stubs_itself(self, agents_dir):
        _write_spec(agents_dir, servers={"granted": {"command": "/bin/g"}}, tools=["@granted"])
        stubs = [{"name": "granted", "command": "/stub", "args": [], "env": [], "type": "stdio"}]
        mirror = ClaudeCodeMirror()
        projection = mirror.session_projection(
            "kirocrew",
            stub_server_names=("granted",),
            stub_elements=stubs,
            permission_surface_owned=True,
        )
        by_name = _by_name(projection.params["mcpServers"])
        assert by_name["granted"]["command"] == "/stub"
        # The wire face IS the projection's params, pinned so the two cannot drift.
        assert (
            mirror.session_params(
                "kirocrew",
                stub_server_names=("granted",),
                stub_elements=stubs,
                permission_surface_owned=True,
            )
            == projection.params
        )

    def test_a_pooled_stub_is_held_to_the_same_tools_allowlist(self, agents_dir):
        """Same rule, same parse, as codex: `tools` gates the stubs too.

        ``*`` still grants all, and a spec with no ``tools`` list grants nothing.
        """
        stubs = [
            {"name": "granted", "command": "/stub", "args": [], "env": [], "type": "stdio"},
            {"name": "unreferenced", "command": "/stub", "args": [], "env": [], "type": "stdio"},
        ]
        mirror = ClaudeCodeMirror()

        def _names(tools):
            _write_spec(agents_dir, servers={"granted": {"command": "/bin/g"}}, tools=tools)
            projection = mirror.session_projection(
                "kirocrew",
                stub_server_names=("granted", "unreferenced"),
                stub_elements=stubs,
                permission_surface_owned=True,
            )
            return {e["name"] for e in projection.params["mcpServers"]}

        assert "granted" in _names(["@granted"])
        assert "unreferenced" not in _names(["@granted"])
        assert {"granted", "unreferenced"} <= _names(["*"])
        assert _names(None) & {"granted", "unreferenced"} == set()

    def test_an_unowned_permission_surface_withholds_the_stubs_too(self, agents_dir):
        """The fail-closed rule covers both halves of the array.

        A stub is a WORKING server (``spawn_run``, ``cron_add``, every pooled
        backend), so appending it after the translated half was withheld would
        hand an ungoverned permission surface exactly the tools the withhold
        exists to keep off it.
        """
        _write_spec(agents_dir, servers={"granted": {"command": "/bin/g"}}, tools=["@granted"])
        stubs = [{"name": "granted", "command": "/stub", "args": [], "env": [], "type": "stdio"}]
        projection = ClaudeCodeMirror().session_projection(
            "kirocrew",
            stub_server_names=("granted",),
            stub_elements=stubs,
            permission_surface_owned=False,
        )
        assert projection.params == {"mcpServers": []}

    def test_a_narrowed_servers_stub_stays_mounted_unlike_codex(self, agents_dir):
        """Claude honours `disabledTools` through `permissions.deny`, not withholding.

        The deny rules in ``settings.local.json`` are keyed
        ``mcp__<server>__<tool>`` and the stub registers under the same server
        name, so the narrowing still applies to it. Withholding the stub, as
        codex must (it has no deny channel), would drop a server this backend
        can narrow correctly.
        """
        _write_spec(
            agents_dir,
            servers={"narrowed": {"command": "/bin/n", "disabledTools": ["dangerous"]}},
            tools=["@narrowed"],
        )
        stubs = [{"name": "narrowed", "command": "/stub", "args": [], "env": [], "type": "stdio"}]
        projection = ClaudeCodeMirror().session_projection(
            "kirocrew",
            stub_server_names=("narrowed",),
            stub_elements=stubs,
            permission_surface_owned=True,
        )
        assert "narrowed" in {e["name"] for e in projection.params["mcpServers"]}


def _mirror_classes():
    from kiro_crew.providers.mirrors.codex import CodexMirror
    from kiro_crew.providers.mirrors.goose import GooseMirror
    from kiro_crew.providers.mirrors.opencode import OpenCodeMirror

    return [ClaudeCodeMirror, CodexMirror, OpenCodeMirror, GooseMirror]


class TestDisabledServersPooledStub:
    """A server switched off WHOLE never mounts through its pooled broker stub.

    The stub carries the same name as the spec entry it rewrites, so the stub half
    of the array is where a ``disabled: true`` server could come back. Every
    mirror holds a stub to the spec's ``tools`` allowlist, and that allowlist never
    grants a disabled server, whichever source switched it off and whatever
    ``tools`` says. An enabled pooled server beside it stays mounted.
    """

    _STUBS = (
        {"name": "my-server", "command": "/stub", "args": [], "env": [], "type": "stdio"},
        {"name": "ok", "command": "/stub", "args": [], "env": [], "type": "stdio"},
    )

    @staticmethod
    def _switch_off(agents_dir: Path, tmp_path: Path, *, source: str, tools: list) -> None:
        disabled = {"command": "/bin/x", "disabled": True}
        servers = {"my-server": dict(disabled) if source == "spec" else {"command": "/bin/x"}}
        servers["ok"] = {"command": "/bin/ok"}
        _write_spec(agents_dir, servers=servers, tools=tools)
        if source == "global":
            (tmp_path / "settings-mcp.json").write_text(
                json.dumps({"mcpServers": {"my-server": disabled}}), encoding="utf-8"
            )

    @pytest.mark.parametrize("mirror_cls", _mirror_classes(), ids=lambda c: c.__name__)
    @pytest.mark.parametrize("source", ["spec", "global"])
    @pytest.mark.parametrize("tools", [["@my-server", "@ok"], ["*"]], ids=["referenced", "star"])
    def test_every_mirror_withholds_a_disabled_servers_stub(
        self, tmp_path, agents_dir, mirror_cls, source, tools
    ):
        self._switch_off(agents_dir, tmp_path, source=source, tools=tools)
        projection = mirror_cls().session_projection(
            "kirocrew",
            stub_server_names=("my-server", "ok"),
            stub_elements=[dict(s) for s in self._STUBS],
            permission_surface_owned=True,
        )
        names = {e.get("name") for e in projection.params["mcpServers"]}
        assert "my-server" not in names
        assert "my-server" in projection.disabled_servers
        assert "ok" in names

    @pytest.mark.parametrize("overlay_disabled", [False, True], ids=["plain", "flagged"])
    def test_the_claude_session_array_omits_a_disabled_servers_stub(
        self, tmp_path, agents_dir, overlay_disabled
    ):
        """The whole client path: overlay stubs in, session array out."""
        from kiro_crew.mcp_gateway.rewriter import _WRAPPER_MARKER

        self._switch_off(agents_dir, tmp_path, source="spec", tools=["@my-server", "@ok"])
        overlay = tmp_path / "gw" / "mcp-gateway" / "agents"
        overlay.mkdir(parents=True)
        wrapped = {
            name: {
                _WRAPPER_MARKER: True,
                "command": "/stub",
                "args": [f"--target-command={name}-cmd"],
                "env": {},
            }
            for name in ("my-server", "ok")
        }
        if overlay_disabled:
            wrapped["my-server"]["disabled"] = True
        (overlay / "kirocrew.json").write_text(
            json.dumps({"name": "kirocrew", "mcpServers": wrapped}), encoding="utf-8"
        )
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._write_claude_local_settings()
        assert client._claude_settings_authored is True
        client._mcp_gateway_overlay = overlay
        assert {s["name"] for s in client._pooled_broker_stubs()} == {"my-server", "ok"}
        names = {e["name"] for e in client._session_mcp_servers()}
        assert "my-server" not in names
        assert "ok" in names


class TestLocalSettingsSeed:
    """Crew's seed of ``<work_dir>/.claude/settings.local.json``.

    The governing rule is ownership, and ownership is NOT the path -- a path under
    a checked-out repository is not Crew's to claim. It is having CREATED the file
    AND the bytes on disk still being the ones Crew wrote. Both hold: Crew may
    overwrite (which is what lets a model-substitution re-seed change the resolved
    model) and reset removes it. Either fails: Crew leaves the path entirely alone,
    and reset removes nothing. Absent: Crew creates it with ``O_EXCL``.

    Nothing here reads, merges into, rewrites or deletes a file Crew did not
    author, which is what keeps a seam that writes into a checked-out project from
    needing a snapshot or a restore write on teardown.

    The CROSS-SESSION half of the same rule -- recognizing a seed a killed session
    left behind, by the digest recorded in
    :mod:`kiro_crew.acp.seed_provenance` -- lives in
    ``test_acp_seed_provenance.py``.
    """

    def _client(self, tmp_path, **kw):
        return AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE, **kw)

    @staticmethod
    def _teardown(client):
        """Tear a client down the way every real caller does.

        Removing the seed is a DISK operation -- a durable revoke of the provenance
        grant, then the unlink -- so it lives in the async
        ``_discard_claude_settings_seed`` and reaches the filesystem through
        ``asyncio.to_thread``. ``_reset_state`` stays synchronous and keeps only the
        in-memory claim release, so calling it alone deliberately leaves the file
        behind.
        """
        asyncio.run(client._discard_claude_settings_seed())
        client._reset_state()

    @staticmethod
    def _advertised(monkeypatch, ids=("global.anthropic.claude-opus-5[1m]",)):
        """Warm the advertised-model cache, the ONLY source the seed reads.

        The seed writes ``availableModels``/``model`` only once the backend has
        actually advertised a list; on a cold cache it deliberately writes neither
        (see ``test_seed_omits_both_model_keys_on_a_cold_cache``). Tests that assert
        on those keys therefore have to stand in for a captured ``session/new``.
        """
        from kiro_crew import model_registry

        monkeypatch.setattr(model_registry, "_ADVERTISED_MODELS", {"claude_code": list(ids)})

    def test_seed_writes_the_model_allowlist(self, tmp_path, monkeypatch):
        # Another settings source defines availableModels, so the warm cache is written.
        monkeypatch.setattr(
            "kiro_crew.acp.client_claude_seed.other_sources_define_available_models",
            lambda *a, **k: True,
        )
        from kiro_crew import model_registry

        self._advertised(monkeypatch)
        client = self._client(tmp_path)
        client._write_claude_local_settings()
        data = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
        # Without the allowlist the adapter can collapse a versioned [1m] id back
        # to the 200K window. The seed writes the window-deduped list (a 200K base
        # id is dropped when its 1M sibling is present), so it can differ from the
        # raw advertised list — compare against seed_available_models, the deduped
        # source the seed actually uses.
        assert data["availableModels"] == model_registry.seed_available_models("claude_code")

    def test_warm_cache_with_no_other_source_writes_the_model_without_the_allowlist(
        self, tmp_path, monkeypatch
    ):
        """An allowlist entry naming a specific id disables the family alias wildcard.

        With no other settings source defining ``availableModels``, the seed omits
        the allowlist, so ``haiku`` keeps resolving to its current model. The pinned
        ``model`` is still written, because a model key does not narrow any alias.
        """
        from kiro_crew import model_registry

        monkeypatch.setattr(
            "kiro_crew.acp.client_claude_seed.other_sources_define_available_models",
            lambda *a, **k: False,
        )
        monkeypatch.setattr(
            model_registry,
            "_ADVERTISED_MODELS",
            {"claude_code": ["default", "opus", "haiku", "claude-haiku-4-5-20251001"]},
        )
        client = self._client(tmp_path, model="haiku", permission_mode="default")
        data = json.loads(client._render_claude_settings_payload())
        assert "availableModels" not in data
        assert data["model"] == "haiku"

    def test_warm_cache_with_another_source_writes_the_seeded_list(self, tmp_path, monkeypatch):
        from kiro_crew import model_registry

        monkeypatch.setattr(
            "kiro_crew.acp.client_claude_seed.other_sources_define_available_models",
            lambda *a, **k: True,
        )
        monkeypatch.setattr(
            model_registry,
            "_ADVERTISED_MODELS",
            {"claude_code": ["default", "opus", "haiku", "claude-haiku-4-5-20251001"]},
        )
        client = self._client(tmp_path, model="haiku", permission_mode="default")
        data = json.loads(client._render_claude_settings_payload())
        assert data["availableModels"] == model_registry.seed_available_models("claude_code")

    def test_seed_omits_both_model_keys_on_a_cold_cache(self, tmp_path, monkeypatch):
        """A cold cache seeds NO model keys — the fix, not a degradation.

        The adapter merges ``availableModels`` union+dedup across settings sources,
        so seeding a list Crew guessed (the old static-registry fallback) REPLACED
        the adapter's own provider-derived list with a staler one: a model the
        registry had not caught up on contributed no ``[1m]`` id, so the pick
        resolved to 200K. And a ``model`` key naming nothing in the list shipped
        beside it is the same failure by another route. Writing neither leaves the
        adapter on its own list, which already carries the versioned ids.
        """
        from kiro_crew import model_registry

        monkeypatch.setattr(model_registry, "_ADVERTISED_MODELS", {})
        client = self._client(tmp_path, model="claude-opus-5", permission_mode="default")
        client._write_claude_local_settings()
        data = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
        assert "availableModels" not in data
        assert "model" not in data
        # The permission surface is NOT deferred: it has to be on disk before
        # session/new, which is the whole reason the seed runs at spawn.
        assert data["permissions"]["defaultMode"] == "default"

    def test_seeded_model_is_folded_onto_an_advertised_id(self, tmp_path, monkeypatch):
        # Another settings source defines availableModels, so the list is written too and
        # the folded model can be checked against it.
        monkeypatch.setattr(
            "kiro_crew.acp.client_claude_seed.other_sources_define_available_models",
            lambda *a, **k: True,
        )
        # The exact shape observed in the field: "model": "claude-opus-5", a bare
        # id the adapter never advertised, which resolves to 200K. A cold cache
        # writes no model key at all.
        from kiro_crew import model_registry

        monkeypatch.setattr(model_registry, "_ADVERTISED_MODELS", {})
        client = self._client(tmp_path, model="claude-opus-5")
        client._write_claude_local_settings()
        path = tmp_path / ".claude" / "settings.local.json"
        assert "model" not in json.loads(path.read_text())

        # Same client, cache now warm (its own session/new was captured): the
        # re-seed writes both, and the model folds onto the advertised spelling.
        self._advertised(monkeypatch)
        client._model = model_registry.resolve_wire_model_id("claude-opus-5", "claude_code")
        client._write_claude_local_settings()
        data = json.loads(path.read_text())
        assert data["model"] == "global.anthropic.claude-opus-5[1m]"
        assert data["model"] in data["availableModels"]

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

    def test_resolved_model_written_but_auto_omitted(self, tmp_path, monkeypatch):
        # Another settings source defines availableModels, so the warm cache is written.
        monkeypatch.setattr(
            "kiro_crew.acp.client_claude_seed.other_sources_define_available_models",
            lambda *a, **k: True,
        )
        self._advertised(monkeypatch, ["claude-sonnet-4-5"])
        auto = self._client(tmp_path)
        auto._write_claude_local_settings()
        path = tmp_path / ".claude" / "settings.local.json"
        assert "model" not in json.loads(path.read_text())
        # A second client only writes when the path is free, so clear the first
        # session's file the way its own reset would -- including handing back
        # its live claim, which the create path now refuses to author under.
        auto._claude_settings_authored = False
        path.unlink()
        from kiro_crew.acp import seed_provenance

        seed_provenance.release(path, auto._seed_owner)
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
        self._teardown(client)
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

        self._teardown(client)
        assert path.read_text() == original

    def test_an_inherited_bypass_mode_is_left_to_its_owner(self, tmp_path):
        """The disclosed cost of not touching a file Crew did not author.

        ``bypassPermissions`` takes every tool call out of the host gate, and Crew
        does not strip it -- stripping meant reading and rewriting a path a
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
        # Nothing was authored, so teardown has nothing to undo.
        assert client._claude_settings_authored is False
        self._teardown(client)

    def test_reset_without_a_seed_removes_nothing(self, tmp_path):
        path = tmp_path / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True)
        path.write_text("{}", encoding="utf-8")
        client = self._client(tmp_path)
        self._teardown(client)
        assert path.exists()

    def test_a_reseed_overwrites_the_file_this_session_created(self, tmp_path, monkeypatch):
        """The model-substitution retry has to be able to change what it wrote.

        ``_new_session_following_substitution`` adopts the gateway-served model and
        re-seeds so the fresh ``SettingsManager`` the adapter builds for the retry
        resolves it. Declining here -- because the path now holds a file -- would
        send byte-identical ``session/new`` params, take the same substitution
        advisory, and fail the session with "even after adopting substitute model".
        """
        # Another settings source defines availableModels, so the warm cache is written.
        monkeypatch.setattr(
            "kiro_crew.acp.client_claude_seed.other_sources_define_available_models",
            lambda *a, **k: True,
        )
        self._advertised(monkeypatch, ["claude-sonnet-4-5"])
        client = self._client(tmp_path)
        client._write_claude_local_settings()
        path = tmp_path / ".claude" / "settings.local.json"
        assert "model" not in json.loads(path.read_text())

        client._model = "claude-sonnet-4-5"
        client._write_claude_local_settings()

        assert json.loads(path.read_text())["model"] == "claude-sonnet-4-5"
        assert client._claude_settings_authored is True
        # And it is still Crew's to remove.
        self._teardown(client)
        assert not path.exists()

    def test_a_reseed_names_a_changed_model_without_another_source(self, tmp_path, monkeypatch):
        """The substitution retry re-seeds with the substitute, so the model key follows.

        The retried ``session/new`` must differ from the first attempt, so the model key
        cannot depend on another settings source defining a list. With none, the first
        seed names no model and no allowlist; once the session's model changes to the
        substitute, the re-seed writes that model and still no ``availableModels``.
        """
        monkeypatch.setattr(
            "kiro_crew.acp.client_claude_seed.other_sources_define_available_models",
            lambda *a, **k: False,
        )
        self._advertised(monkeypatch, ["claude-sonnet-4-5"])
        client = self._client(tmp_path)
        client._write_claude_local_settings()
        path = tmp_path / ".claude" / "settings.local.json"
        first = json.loads(path.read_text())
        assert "model" not in first
        assert "availableModels" not in first

        client._model = "claude-sonnet-4-5"
        client._write_claude_local_settings()

        data = json.loads(path.read_text())
        assert data["model"] == "claude-sonnet-4-5"
        assert "availableModels" not in data
        assert client._claude_settings_authored is True
        # And it is still Crew's to remove.
        self._teardown(client)
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
        client._claude_settings_claim_unrevoked = True
        client._reset_state()  # must not raise
        # The reset ran past the read, and absent stayed "authored nothing".
        assert client._claude_settings_claim_unrevoked is False
        assert not hasattr(client, "_claude_settings_authored")
