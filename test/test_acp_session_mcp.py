"""Session-array MCP wiring: agent spec -> session/new mcpServers array.

A backend in ``ACP_BACKENDS_SESSION_MCP_ARRAY`` (claude-agent-acp today) receives
its MCP servers ONLY through the ``session/new`` / ``session/load`` parameter, so
these tests pin the shape the adapter's schema requires (``env``/``headers`` always arrays, an explicit transport ``type``) and
the mounting rules the kiro agent spec expresses (``tools`` references, the
registry pointer, Crew's own control plane).
"""

from __future__ import annotations

import asyncio
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
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION
from kiro_crew.providers.mirrors import claude_code as claude_mirror
from kiro_crew.providers.mirrors import registry as mirrors_registry
from kiro_crew.providers.mirrors.claude_code import ClaudeCodeMirror

_CORE = {"command": "/opt/kirocrew", "args": ["mcp-core"]}
_CRON = {"command": "/opt/kirocrew", "args": ["mcp-cron"]}


@pytest.fixture(autouse=True)
def _pinned_kiro_cli_version(monkeypatch):
    """Pin the kiro-cli release the spec ``permissions`` gate believes is installed.

    A client start here materialises the agent spec (``ensure_agent_materialized``
    -> ``rebuild_agent_config`` -> ``_write_derived_permissions``), which reads
    ``installed_kiro_cli_version`` function-locally from ``kiro_crew.kiro_cli``:
    one real ``kiro-cli --version`` spawn per binary identity, process-cached, so
    whichever test in the worker starts first pays it against the HOST's install
    with the checkout as the child's cwd. Pinned to the floor release, as
    ``test_agent.py`` and the generated-writer suites pin it.
    """
    monkeypatch.setattr(
        "kiro_crew.kiro_cli.installed_kiro_cli_version",
        lambda: SPEC_PERMISSIONS_MIN_VERSION,
    )


@pytest.fixture(autouse=True)
def _installed_claude_adapter_version(monkeypatch):
    """The installed claude-agent-acp version the spawn arm would have read.

    The settings writer applies the ``settingSources`` floor before the
    handshake from ``_claude_adapter_disk_version``; tests that drive the writer
    directly never run the spawn arm, so the floor release is set as the class
    default. A test about the floor sets its own.
    """
    monkeypatch.setattr(AcpClient, "_claude_adapter_disk_version", "0.84.0", raising=False)


@pytest.fixture(autouse=True)
def isolated_seed_provenance(monkeypatch):
    """Per-test provenance state, mirroring test_acp_seed_provenance.py.

    ``_RECORDS``, ``_LIVE`` and ``_SHARERS`` are process-wide runtime state;
    without this, a client authored in one test leaves a live claim that a
    LATER test's client (pytest truncates long test names to one shared
    tmp-dir prefix, so paths can even collide) is correctly refused against.
    """
    from kiro_crew.acp import seed_provenance

    monkeypatch.setattr(seed_provenance, "_RECORDS", {})
    monkeypatch.setattr(seed_provenance, "_LIVE", {})
    monkeypatch.setattr(seed_provenance, "_SHARERS", {})


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    """Point the agent-spec resolver at a temp agents directory."""
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", d)
    # The global settings file is a second source of per-tool restrictions;
    # these tests supply it (or leave it absent) rather than read the machine's.
    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", tmp_path / "settings-mcp.json")
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


def _write_spec(
    agents_dir: Path, *, servers: dict, tools: list | None, name: str = "kirocrew"
) -> None:
    spec: dict = {"name": name, "mcpServers": servers}
    if tools is not None:
        spec["tools"] = tools
    (agents_dir / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")


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

    @pytest.mark.parametrize("pooled", [False, True])
    def test_claude_projects_the_admitted_direct_or_pooled_server(
        self,
        tmp_path,
        agents_dir,
        pooled,
    ):
        from kiro_crew.mcp_gateway.rewriter import _WRAPPER_MARKER

        _write_spec(
            agents_dir,
            servers={"foo": {"command": "/bin/foo", "args": ["serve"], "env": {"K": "V"}}},
            tools=["@foo"],
        )
        overlay = tmp_path / "broker-overlay"
        overlay.mkdir()
        (overlay / "kirocrew.json").write_text(
            json.dumps(
                {
                    "name": "kirocrew",
                    "mcpServers": {
                        "foo": {
                            _WRAPPER_MARKER: True,
                            "command": "broker-stub",
                            "args": [],
                            "env": {},
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        client = self._seeded(
            tmp_path,
            agent="kirocrew",
            acp_backend=ACP_BACKEND_CLAUDE,
            mcp_gateway_overlay=overlay if pooled else None,
        )
        original = _by_name(client._session_mcp_servers()).get("foo")
        # The mirror keeps the allowlisted entry in both supported transports.
        assert original is not None
        if not pooled:
            assert original["command"] == "/bin/foo"
            assert original["args"] == ["serve"]
            assert original["env"] == [{"name": "K", "value": "V"}]
        else:
            assert original["command"] == "broker-stub"
        # The shared append is inert for claude -- the mirror placed the stubs.
        assert client._pooled_mcp_servers() == []

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
            # ``**_kw`` so the double keeps mirroring the real signature: the call
            # site passes the session's checkout as ``work_dir``, and a double that
            # refuses it turns this seam test into a test of the except branch.
            client_mod,
            "injection_server_names",
            lambda _o, _a, **_kw: frozenset({"pooled"}),
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

        def _boom(_o, _a, **_kw):
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

        monkeypatch.setattr(claude_mirror, "session_mcp_projection", _never)
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
        real = session_mcp.session_mcp_projection

        def _counted(*a, **kw):
            calls.append(1)
            return real(*a, **kw)

        monkeypatch.setattr(claude_mirror, "session_mcp_projection", _counted)
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        assert "foo" in _by_name(client._session_mcp_servers())
        assert "foo" in _by_name(client._session_mcp_servers())
        assert len(calls) == 1
        client._reset_state()
        assert client._session_mcp_cache is None
        client._write_claude_local_settings()  # the next spawn re-seeds before resolving
        assert "foo" in _by_name(client._session_mcp_servers())
        assert len(calls) == 2

    @staticmethod
    def _project_owned_settings(tmp_path, payload):
        """A project's OWN ``.claude/settings.local.json``, written before Crew runs."""
        path = tmp_path / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    @staticmethod
    async def _session_new_params(client, tmp_path, monkeypatch):
        """Drive the real ``session/new`` call site and return the params it sent."""
        sent: list[dict] = []

        async def _work_dir():
            return str(tmp_path)

        async def _send(_method, params):
            sent.append(params)
            return 1

        async def _wait(_rid, **_kw):
            return {"sessionId": "s-1"}

        monkeypatch.setattr(client, "_session_work_dir", _work_dir)
        monkeypatch.setattr(client, "_send_request", _send)
        monkeypatch.setattr(client, "_wait_for_response", _wait)
        await client._new_session_following_substitution()
        assert len(sent) == 1
        return sent[0]

    @pytest.mark.asyncio
    async def test_a_project_owned_settings_file_still_gets_crews_tools(
        self, tmp_path, agents_dir, monkeypatch
    ):
        """A project that owns settings.local.json keeps Crew's tools, gated.

        The file is left exactly as it is, and out of the session: the envelope
        loads only the ``user`` setting source, so neither the project's
        ``settings.local.json`` nor its checked-in ``.claude/settings.json`` reaches
        the CLI. No ``permissions.allow`` a repository carries can pre-approve a
        call before Crew's gate sees it. Crew's own settings ride inline instead.
        That is what makes it safe to deliver the whole array -- so a cron job's
        ``send_message`` reaches this session too.
        """
        _write_spec(
            agents_dir,
            servers={"foo": {"command": "/bin/foo", "disabledTools": ["danger"]}},
            tools=["@foo"],
        )
        original = {"permissions": {"allow": ["mcp__foo__write"]}}
        path = self._project_owned_settings(tmp_path, original)
        # The checked-in project tier carries an allow rule of its own.
        (tmp_path / ".claude" / "settings.json").write_text(
            json.dumps({"permissions": {"allow": ["mcp__foo__read"]}}), encoding="utf-8"
        )

        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._write_claude_local_settings()
        assert client._claude_settings_authored is False
        assert json.loads(path.read_text()) == original

        params = await self._session_new_params(client, tmp_path, monkeypatch)

        assert "foo" in _by_name(params["mcpServers"])
        options = params["_meta"]["claudeCode"]["options"]
        # Neither project tier loads: no local, and no checked-in project file.
        assert options["settingSources"] == ["user"]
        assert options["allowDangerouslySkipPermissions"] is False
        # Neither allow rule reaches the session through any channel.
        assert "mcp__foo__write" not in json.dumps(params)
        assert "mcp__foo__read" not in json.dumps(params)
        # Crew's own restriction does, through the inline settings tier.
        assert "mcp__foo__danger" in options["settings"]["permissions"]["deny"]
        # And the project's file is still exactly the project's.
        assert json.loads(path.read_text()) == original
        await client._discard_claude_settings_seed()
        client._reset_state()
        assert json.loads(path.read_text()) == original

    def test_the_project_tiers_deny_rules_ride_inline(self, tmp_path, agents_dir):
        """Leaving the project tiers out drops their allows, never their denies.

        Both project files' ``permissions.deny`` rules are carried in the inline
        settings beside Crew's own, so a repository's refusal still holds.
        """
        _write_spec(
            agents_dir,
            servers={"foo": {"command": "/bin/foo", "disabledTools": ["danger"]}},
            tools=["@foo"],
        )
        self._project_owned_settings(
            tmp_path,
            {"permissions": {"allow": ["mcp__foo__write"], "deny": ["Bash(git push:*)"]}},
        )
        (tmp_path / ".claude" / "settings.json").write_text(
            json.dumps({"permissions": {"deny": ["Bash(rm:*)", "Bash(git push:*)"]}}),
            encoding="utf-8",
        )
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._write_claude_local_settings()
        assert "foo" in _by_name(client._session_mcp_servers())
        options = client._claude_session_meta()["claudeCode"]["options"]
        deny = options["settings"]["permissions"]["deny"]
        assert "mcp__foo__danger" in deny
        assert "Bash(git push:*)" in deny
        assert "Bash(rm:*)" in deny
        assert deny.count("Bash(git push:*)") == 1
        assert "mcp__foo__write" not in json.dumps(options)

    @pytest.mark.parametrize(
        "shape", ["directory", "oversized", "fifo", "deep", "hooks", "sandbox"]
    )
    def test_unexaminable_project_denies_withhold_the_array(self, tmp_path, agents_dir, shape):
        """A deny file whose bytes cannot be examined never drops silently: no tools ship."""
        if shape == "fifo" and not hasattr(os, "mkfifo"):
            pytest.skip("needs os.mkfifo")
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        self._project_owned_settings(tmp_path, {"permissions": {}})
        checked_in = tmp_path / ".claude" / "settings.json"
        if shape == "directory":
            checked_in.mkdir()
        elif shape == "fifo":
            os.mkfifo(checked_in)
        elif shape == "hooks":
            hook = {"type": "command", "command": "./block.sh"}
            checked_in.write_text(
                json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [hook]}]}}),
                encoding="utf-8",
            )
        elif shape == "sandbox":
            checked_in.write_text(json.dumps({"sandbox": {"enabled": True}}), encoding="utf-8")
        elif shape == "deep":
            depth = 200_000
            checked_in.write_text(
                '{"permissions": {"deny": ["Bash(rm:*)"]}, "x": ' + "[" * depth + "]" * depth + "}",
                encoding="utf-8",
            )
        else:
            checked_in.write_bytes(b" " * (client_mod._CLAUDE_PROJECT_SETTINGS_MAX_BYTES + 1))
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._write_claude_local_settings()
        assert client._claude_local_settings_excluded is False
        assert client._session_mcp_servers() == []

    def test_the_project_tiers_ask_rules_ride_inline(self, tmp_path, agents_dir):
        """An ``ask`` rule forces a prompt, so leaving its tier out must not drop it."""
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        self._project_owned_settings(tmp_path, {"permissions": {"ask": ["Bash(git push:*)"]}})
        (tmp_path / ".claude" / "settings.json").write_text(
            json.dumps({"permissions": {"ask": ["mcp__foo__write", "Bash(git push:*)"]}}),
            encoding="utf-8",
        )
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._write_claude_local_settings()
        assert "foo" in _by_name(client._session_mcp_servers())
        ask = client._claude_session_meta()["claudeCode"]["options"]["settings"]["permissions"][
            "ask"
        ]
        assert ask == ["Bash(git push:*)", "mcp__foo__write"]

    @pytest.mark.asyncio
    async def test_an_ask_rule_gained_before_the_first_prompt_stops_the_session(
        self, tmp_path, agents_dir, monkeypatch
    ):
        client = self._excluded_client(tmp_path, agents_dir, {"permissions": {}})
        sent, killed = self._adapter_transport(client, monkeypatch, started_mode="default")
        real_send = client._send_request

        async def _send(method, params):
            rid = await real_send(method, params)
            if method == "session/new":
                (tmp_path / ".claude" / "settings.json").write_text(
                    json.dumps({"permissions": {"ask": ["Bash(git push:*)"]}}), encoding="utf-8"
                )
            return rid

        monkeypatch.setattr(client, "_send_request", _send)
        with pytest.raises(client_mod.AcpError, match="gained deny or ask rules"):
            await client._initialize_session()
        assert killed == [True]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("version", ["0.84.0", "0.83.0", None])
    async def test_the_handshake_never_resolves_the_array_on_the_loop(
        self, tmp_path, agents_dir, monkeypatch, version
    ):
        """The floor is decided before the array is warmed, so nothing rebuilds it.

        Once the spawn arm has warmed the projection, the handshake -- whatever
        version the adapter reports -- must never fall back to the inline
        resolve, which reads disk on the gateway loop.
        """
        client = self._excluded_client(tmp_path, agents_dir, {"permissions": {}})

        def _on_loop_resolve():
            raise AssertionError("the MCP array was re-resolved on the event loop")

        monkeypatch.setattr(client, "_resolve_session_mcp_servers", _on_loop_resolve)
        _sent, _killed = self._adapter_transport(
            client, monkeypatch, started_mode="default", version=version
        )
        try:
            await client._initialize_session()
        except client_mod.AcpError as exc:
            assert "not verified to honour settingSources" in str(exc)
            assert version != "0.84.0"

    @pytest.mark.parametrize(
        "checked_in",
        [
            "{not json",
            json.dumps(["not", "a", "dict"]),
            json.dumps({"permissions": "nope"}),
            json.dumps({"permissions": {"deny": "Bash(rm:*)"}}),
        ],
    )
    def test_a_file_claude_takes_no_denies_from_adds_none(self, tmp_path, agents_dir, checked_in):
        """Content claude could not take deny rules from contributes none, and blocks nothing."""
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        self._project_owned_settings(tmp_path, {"permissions": {}})
        (tmp_path / ".claude" / "settings.json").write_text(checked_in, encoding="utf-8")
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._write_claude_local_settings()
        assert "foo" in _by_name(client._session_mcp_servers())
        assert "Bash(rm:*)" not in json.dumps(client._claude_session_meta())

    @pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
    def test_a_linked_checked_in_settings_file_withholds_the_array(self, tmp_path, agents_dir):
        """A checked-in ``settings.json`` that is a link is never read through."""
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        self._project_owned_settings(tmp_path, {"permissions": {}})
        target = tmp_path / "elsewhere.json"
        target.write_text(json.dumps({"permissions": {"deny": ["Bash(rm:*)"]}}), encoding="utf-8")
        try:
            (tmp_path / ".claude" / "settings.json").symlink_to(target)
        except OSError:
            pytest.skip("symlink creation not permitted")
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._write_claude_local_settings()
        assert client._session_mcp_servers() == []

    def test_each_write_redecides_the_exclusion(self, tmp_path, agents_dir):
        """A later run where Crew authors the file drops the earlier exclusion."""
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        path = self._project_owned_settings(tmp_path, {"permissions": {}})
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._write_claude_local_settings()
        assert client._claude_local_settings_excluded is True
        path.unlink()
        client._write_claude_local_settings()
        assert client._claude_settings_authored is True
        assert client._claude_local_settings_excluded is False
        assert client._claude_inline_settings is None
        assert "foo" in _by_name(client._session_mcp_servers())
        assert client._claude_session_meta() == {"claudeCode": {"options": {}}}

    def test_the_envelope_follows_the_array_it_shipped_with(self, tmp_path, agents_dir):
        """An array projected under the exclusion carries it even once the flag clears."""
        _write_spec(
            agents_dir,
            servers={"foo": {"command": "/bin/foo", "disabledTools": ["danger"]}},
            tools=["@foo"],
        )
        self._project_owned_settings(tmp_path, {"permissions": {}})
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._write_claude_local_settings()
        assert "foo" in _by_name(client._session_mcp_servers())
        client._claude_local_settings_excluded = False
        client._claude_inline_settings = None
        options = client._claude_session_meta()["claudeCode"]["options"]
        assert options["settingSources"] == ["user"]
        assert options["allowDangerouslySkipPermissions"] is False
        assert "mcp__foo__danger" in options["settings"]["permissions"]["deny"]

    @pytest.mark.asyncio
    async def test_the_substitution_retry_rebuilds_the_envelope(
        self, tmp_path, agents_dir, monkeypatch
    ):
        """The retry's envelope comes from the retry's own re-seed.

        The first ``session/new`` is sent before the project file exists, under
        Crew's own seed. The file then appears, and the retry leaves it out: the
        array it re-derives must travel with ``settingSources`` minus ``local``
        and with the substitute model inline, never with the first attempt's
        envelope.
        """
        from kiro_crew import model_registry

        substitute = "global.anthropic.claude-sonnet-4-6[1m]"
        monkeypatch.setattr(
            model_registry,
            "_ADVERTISED_MODELS",
            {"claude_code": ["global.anthropic.claude-opus-5[1m]", substitute]},
        )
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = AcpClient(
            work_dir=tmp_path,
            agent="kirocrew",
            acp_backend=ACP_BACKEND_CLAUDE,
            model="global.anthropic.claude-opus-5[1m]",
        )
        client._write_claude_local_settings()
        assert client._claude_settings_authored is True
        path = tmp_path / ".claude" / "settings.local.json"
        sent: list[dict] = []

        async def _work_dir():
            return str(tmp_path)

        async def _send(_method, params):
            sent.append(json.loads(json.dumps(params)))
            if len(sent) == 1:
                # Crew's seed goes away and the project's own file takes the path.
                client._claude_settings_authored = False
                client._claude_settings_written = None
                path.unlink()
                path.write_text(
                    json.dumps({"permissions": {"allow": ["mcp__foo__write"]}}), encoding="utf-8"
                )
            return len(sent)

        async def _wait(_rid, **_kw):
            if len(sent) == 1:
                client._last_substitution_model = substitute
                return {}
            return {"sessionId": "s-1"}

        monkeypatch.setattr(client, "_session_work_dir", _work_dir)
        monkeypatch.setattr(client, "_send_request", _send)
        monkeypatch.setattr(client, "_wait_for_response", _wait)
        await client._new_session_following_substitution()

        assert len(sent) == 2
        assert sent[0]["_meta"] == {"claudeCode": {"options": {}}}
        retry = sent[1]
        assert "foo" in _by_name(retry["mcpServers"])
        options = retry["_meta"]["claudeCode"]["options"]
        assert options["settingSources"] == ["user"]
        assert options["settings"]["model"] == substitute
        assert "mcp__foo__write" not in json.dumps(retry)

    @pytest.mark.asyncio
    async def test_a_crew_authored_seed_keeps_every_setting_source(
        self, tmp_path, agents_dir, monkeypatch
    ):
        """The normal case is unchanged: Crew's own file IS the local tier."""
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        params = await self._session_new_params(client, tmp_path, monkeypatch)
        assert "foo" in _by_name(params["mcpServers"])
        assert params["_meta"] == {"claudeCode": {"options": {}}}

    @staticmethod
    def _pin_harness(client, monkeypatch, *, fail=False):
        """Record the requests the pin sends; optionally make ``set_mode`` fail."""
        sent: list[tuple[str, dict]] = []
        killed: list[bool] = []

        async def _send(method, params):
            sent.append((method, params))
            return len(sent)

        async def _wait(_rid, **_kw):
            if fail:
                raise client_mod.AcpError("set_mode refused")
            return {}

        async def _kill(*, force=False):
            killed.append(force)

        monkeypatch.setattr(client, "_send_request", _send)
        monkeypatch.setattr(client, "_wait_for_response", _wait)
        monkeypatch.setattr(client, "_kill_process", _kill)
        return sent, killed

    def _excluded_client(self, tmp_path, agents_dir, payload):
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        self._project_owned_settings(tmp_path, payload)
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._write_claude_local_settings()
        assert "foo" in _by_name(client._session_mcp_servers())
        return client

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["bypassPermissions", "auto", None])
    async def test_a_session_started_in_a_gate_escaping_mode_is_pinned(
        self, tmp_path, agents_dir, monkeypatch, mode
    ):
        """The mode is read back from the harness, never from the mutable file.

        claude-agent-acp picks the starting mode from every settings file itself.
        So whatever the session reports -- or fails to report -- it is pinned to
        the asking mode before the first prompt.
        """
        client = self._excluded_client(tmp_path, agents_dir, {"permissions": {}})
        sent, killed = self._pin_harness(client, monkeypatch)
        resp = {"sessionId": "s-1"}
        if mode is not None:
            resp["modes"] = {"currentModeId": mode}
        await client._pin_claude_starting_mode(resp)
        assert sent == [("session/set_mode", {"sessionId": "s-1", "modeId": "default"})]
        assert killed == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["default", "plan", "acceptEdits", "dontAsk"])
    async def test_a_mode_that_still_asks_about_crew_tools_is_kept(
        self, tmp_path, agents_dir, monkeypatch, mode
    ):
        """An operator's stricter or narrower mode is never widened to ``default``.

        ``plan`` is read-only and ``acceptEdits`` approves only file edits; each
        still asks the host about an MCP call. ``dontAsk`` refuses what is not
        pre-approved, so it runs nothing ``default`` would not. Each is left
        exactly as the session started it.
        """
        client = self._excluded_client(tmp_path, agents_dir, {"permissions": {}})
        sent, killed = self._pin_harness(client, monkeypatch)
        await client._pin_claude_starting_mode(
            {"sessionId": "s-1", "modes": {"currentModeId": mode}}
        )
        assert sent == []
        assert killed == []

    @pytest.mark.asyncio
    async def test_a_pin_that_fails_stops_the_session(self, tmp_path, agents_dir, monkeypatch):
        """Crew's tools never run under a mode that approves on its own."""
        client = self._excluded_client(tmp_path, agents_dir, {"permissions": {}})
        _sent, killed = self._pin_harness(client, monkeypatch, fail=True)
        with pytest.raises(client_mod.AcpError, match="pin the asking permission mode"):
            await client._pin_claude_starting_mode(
                {"sessionId": "s-1", "modes": {"currentModeId": "auto"}}
            )
        assert killed == [True]

    @pytest.mark.asyncio
    async def test_a_file_swapped_after_the_check_cannot_keep_an_approving_mode(
        self, tmp_path, agents_dir, monkeypatch
    ):
        """A safe file at check time, an approving one at session/new.

        The array ships, because nothing about it depends on the file. The mode
        the swapped file picked comes back on the response and is pinned; if the
        pin fails the session is stopped, so it never ends with Crew's tools
        mounted under an approving mode.
        """
        client = self._excluded_client(
            tmp_path, agents_dir, {"permissions": {"defaultMode": "default"}}
        )
        path = tmp_path / ".claude" / "settings.local.json"
        params = await self._session_new_params(client, tmp_path, monkeypatch)
        path.write_text(json.dumps({"permissions": {"defaultMode": "auto"}}), encoding="utf-8")
        assert "foo" in _by_name(params["mcpServers"])
        # Bypass cannot even be the starting mode.
        assert params["_meta"]["claudeCode"]["options"]["allowDangerouslySkipPermissions"] is False

        sent, killed = self._pin_harness(client, monkeypatch)
        await client._pin_claude_starting_mode(
            {"sessionId": "s-1", "modes": {"currentModeId": "auto"}}
        )
        assert sent == [("session/set_mode", {"sessionId": "s-1", "modeId": "default"})]

        _sent, killed = self._pin_harness(client, monkeypatch, fail=True)
        with pytest.raises(client_mod.AcpError):
            await client._pin_claude_starting_mode(
                {"sessionId": "s-1", "modes": {"currentModeId": "auto"}}
            )
        assert killed == [True]

    @staticmethod
    def _recorded_results(scenario):
        """The adapter's own answers, in order, from a live claude-agent-acp 0.84.0 capture.

        ``test/fixtures/claude_mode_pin/<scenario>.jsonl``: agent-to-client lines
        off the wire, pruned of recording-host data (see each file's ``_meta``).
        Returns the JSON-RPC results keyed by the request they answered.
        """
        path = Path(__file__).parent / "fixtures" / "claude_mode_pin" / f"{scenario}.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        head = rows[0]["_meta"]
        assert head["agent_version"] == "0.84.0" and head["recorded"] == "live"
        sent = [label.split(" ")[0] for label in head["client_sent"]]
        results = [row["result"] for row in rows[1:] if "id" in row and "result" in row]
        assert len(results) == len(sent)
        by_method: dict[str, list[dict]] = {}
        for method, result in zip(sent, results):
            by_method.setdefault(method, []).append(result)
        return by_method

    @classmethod
    def _adapter_transport(
        cls,
        client,
        monkeypatch,
        *,
        started_mode,
        version="0.84.0",
        scenario="project-plan-then-auto-then-pin",
    ):
        """Answer the handshake with claude-agent-acp 0.84.0's recorded frames.

        ``initialize``, ``session/new``, ``session/load`` and ``session/set_mode``
        are answered from the live capture. Two fields are set per test and
        named here so nothing else is invented: ``sessionId`` (``s-new``, or
        the id a ``session/load`` asked for), and
        ``modes.currentModeId`` when ``started_mode`` differs from the recorded
        one -- the capture shows a project file cannot START a session in
        ``auto`` or ``bypassPermissions`` on this path (``project-auto-filtered``),
        so those starting modes reach the pin only from another tier. ``version``
        replaces ``agentInfo.version`` for the floor tests, and ``None`` drops it.
        """
        recorded = cls._recorded_results(scenario)
        sent: list[tuple[str, dict]] = []
        killed: list[bool] = []

        def _with_mode(result):
            result = json.loads(json.dumps(result))
            if started_mode is not None:
                result["modes"]["currentModeId"] = started_mode
            return result

        async def _send(method, params):
            sent.append((method, params))
            return len(sent)

        async def _wait(rid, **_kw):
            method = sent[rid - 1][0]
            if method == "initialize":
                resp = json.loads(json.dumps(recorded["initialize"][0]))
                if version is None:
                    resp.pop("agentInfo", None)
                else:
                    resp["agentInfo"]["version"] = version
                return resp
            if method == "session/new":
                return {**_with_mode(recorded["session/new"][0]), "sessionId": "s-new"}
            if method == "session/load":
                # The adapter answers a load for the session it was asked to load.
                return {
                    **_with_mode(recorded["session/load"][0]),
                    "sessionId": sent[rid - 1][1]["sessionId"],
                }
            if method == "session/set_mode":
                return recorded["session/set_mode"][-1]
            return {}

        async def _kill(*, force=False):
            killed.append(force)

        async def _work_dir():
            return str(client._work_dir)

        monkeypatch.setattr(client, "_send_request", _send)
        monkeypatch.setattr(client, "_wait_for_response", _wait)
        monkeypatch.setattr(client, "_kill_process", _kill)
        monkeypatch.setattr(client, "_session_work_dir", _work_dir)
        monkeypatch.setattr(client, "_drain_notifications", mock.AsyncMock())
        return sent, killed

    @pytest.mark.asyncio
    @pytest.mark.parametrize("started_mode", ["default", "plan", "bypassPermissions"])
    @pytest.mark.parametrize("path", ["new", "load"])
    async def test_a_claude_session_actually_starts_under_the_pin(
        self, tmp_path, agents_dir, monkeypatch, started_mode, path
    ):
        """The whole handshake completes, on both establishment paths.

        The pin is not a kill that always fires: the adapter handles
        ``session/set_mode``, so a session that started in ``bypassPermissions``
        is moved to ``default`` and keeps running, and one in ``default`` or
        ``plan`` sends nothing extra.
        """
        client = self._excluded_client(tmp_path, agents_dir, {"permissions": {}})
        if path == "load":
            client._resume_session_id = "s-old"
        sent, killed = self._adapter_transport(client, monkeypatch, started_mode=started_mode)

        await client._initialize_session()

        methods = [m for m, _ in sent]
        assert client._session_id == ("s-old" if path == "load" else "s-new")
        assert client._resumed is (path == "load")
        assert ("session/load" if path == "load" else "session/new") in methods
        assert killed == []
        pins = [p for m, p in sent if m == "session/set_mode"]
        if started_mode in ("default", "plan"):
            # Kept exactly as started: a read-only mode is never widened.
            assert pins == []
        else:
            assert pins == [{"sessionId": client._session_id, "modeId": "default"}]

    @pytest.mark.parametrize(
        "version", ["0.83.0", "0.24.2", "", pytest.param("1" * 5000 + ".0.0", id="huge")]
    )
    def test_an_installed_adapter_below_the_floor_gets_no_tools(
        self, tmp_path, agents_dir, version
    ):
        """An adapter not verified to honour ``settingSources`` never gets the array.

        Below 0.84.0, or with no readable version, the adapter could load the
        project's ``permissions.allow`` anyway. The writer reads the installed
        package's version before the array is first resolved, so the exclusion
        is never taken: no Crew tools and no exclusion envelope, as before.
        """
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        self._project_owned_settings(tmp_path, {"permissions": {}})
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._claude_adapter_disk_version = version
        client._write_claude_local_settings()
        assert client._claude_local_settings_excluded is False
        assert client._session_mcp_servers() == []
        assert client._claude_session_meta() == {"claudeCode": {"options": {}}}

    @pytest.mark.parametrize("version", ["0.84.0", "0.85.2-preview.3", "1.0.0"])
    def test_an_installed_adapter_at_or_above_the_floor_keeps_the_tools(
        self, tmp_path, agents_dir, version
    ):
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        self._project_owned_settings(tmp_path, {"permissions": {}})
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._claude_adapter_disk_version = version
        client._write_claude_local_settings()
        assert "foo" in _by_name(client._session_mcp_servers())
        options = client._claude_session_meta()["claudeCode"]["options"]
        assert options["settingSources"] == ["user"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("version", ["0.83.0", "", None])
    async def test_a_handshake_below_the_floor_stops_an_excluded_session(
        self, tmp_path, agents_dir, monkeypatch, version
    ):
        """The installed package passed the floor, the running adapter did not."""
        client = self._excluded_client(tmp_path, agents_dir, {"permissions": {}})
        _sent, killed = self._adapter_transport(
            client, monkeypatch, started_mode="default", version=version
        )
        with pytest.raises(client_mod.AcpError, match="not verified to honour settingSources"):
            await client._initialize_session()
        assert killed == [True]

    def test_the_spawn_arm_reads_the_version_before_the_seed_and_the_warm(
        self, tmp_path, monkeypatch
    ):
        """Floor first, then the writer, then the warm: the array is never rebuilt.

        Driven through the claude adapter's own launch: the writer must already see
        the installed adapter version (it applies the ``settingSources`` floor with
        it), and the session's MCP array is resolved once, after the writer decided
        whether Crew authored the settings file.
        """
        from kiro_crew.acp.harness import SpawnContext
        from kiro_crew.acp.harness import claude as claude_mod
        from kiro_crew.acp.harness import process_adapter_for
        from kiro_crew.agent_sdk.drivers.acp import forget_cached_resolution

        steps: list[tuple[str, str]] = []
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)

        def _version(argv):
            steps.append(("read", ""))
            return "0.84.0"

        def _write(self):
            steps.append(("seed", self._claude_adapter_disk_version))

        def _resolve(self):
            steps.append(("warm", ""))
            return []

        forget_cached_resolution(ACP_BACKEND_CLAUDE)
        monkeypatch.setattr(
            claude_mod, "_resolve_claude_acp_bin", lambda: (["/opt/bin/claude-agent-acp"], "")
        )
        monkeypatch.setattr(claude_mod, "_claude_adapter_installed_version", _version)
        monkeypatch.setattr(AcpClient, "_write_claude_local_settings", _write)
        monkeypatch.setattr(AcpClient, "_resolve_session_mcp_servers", _resolve)
        try:
            asyncio.run(
                process_adapter_for(ACP_BACKEND_CLAUDE).resolve_spawn(
                    SpawnContext(
                        agent="kirocrew",
                        work_dir=tmp_path,
                        model=None,
                        environ={},
                        home=tmp_path,
                        session=client,
                    )
                )
            )
        finally:
            forget_cached_resolution(ACP_BACKEND_CLAUDE)

        assert steps == [("read", ""), ("seed", "0.84.0"), ("warm", "")]
        assert client._session_mcp_cache == []

    def test_the_installed_version_is_read_from_the_adapters_own_manifest(self, tmp_path):
        pkg = tmp_path / "node_modules" / "@agentclientprotocol" / "claude-agent-acp"
        (pkg / "dist").mkdir(parents=True)
        entry = pkg / "dist" / "index.js"
        entry.write_text("", encoding="utf-8")
        (pkg / "package.json").write_text(
            json.dumps({"name": client_mod.CLAUDE_ACP_NPM_PKG, "version": "0.84.0"}),
            encoding="utf-8",
        )
        assert client_mod._claude_adapter_installed_version(["node", str(entry)]) == "0.84.0"
        (pkg / "package.json").write_text(
            json.dumps({"name": "something-else", "version": "9.9.9"}), encoding="utf-8"
        )
        assert client_mod._claude_adapter_installed_version(["node", str(entry)]) == ""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("change", ["local", "checked_in", "unreadable"])
    async def test_a_deny_gained_before_the_first_prompt_stops_the_session(
        self, tmp_path, agents_dir, monkeypatch, change
    ):
        """A deny added between the writer's read and ``session/new`` is not lost.

        The envelope froze the denies it read; a project file that gained one, or
        became unexaminable, before the first prompt stops the harness.
        """
        client = self._excluded_client(
            tmp_path, agents_dir, {"permissions": {"deny": ["Bash(rm:*)"]}}
        )
        sent, killed = self._adapter_transport(client, monkeypatch, started_mode="default")
        claude_dir = tmp_path / ".claude"
        real_send = client._send_request

        async def _send(method, params):
            rid = await real_send(method, params)
            if method == "session/new":
                if change == "local":
                    (claude_dir / "settings.local.json").write_text(
                        json.dumps({"permissions": {"deny": ["Bash(rm:*)", "Bash(curl:*)"]}}),
                        encoding="utf-8",
                    )
                elif change == "checked_in":
                    (claude_dir / "settings.json").write_text(
                        json.dumps({"permissions": {"deny": ["Bash(curl:*)"]}}), encoding="utf-8"
                    )
                else:
                    (claude_dir / "settings.json").mkdir()
            return rid

        monkeypatch.setattr(client, "_send_request", _send)
        with pytest.raises(client_mod.AcpError, match="gained deny or ask rules"):
            await client._initialize_session()
        assert "Bash(rm:*)" in json.dumps(dict(sent)["session/new"]["_meta"])
        assert killed == [True]

    @pytest.mark.asyncio
    async def test_an_unchanged_project_deny_set_lets_the_session_start(
        self, tmp_path, agents_dir, monkeypatch
    ):
        client = self._excluded_client(
            tmp_path, agents_dir, {"permissions": {"deny": ["Bash(rm:*)"]}}
        )
        _sent, killed = self._adapter_transport(client, monkeypatch, started_mode="default")
        await client._initialize_session()
        assert client._session_id == "s-new"
        assert killed == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("version", ["0.83.0", None])
    async def test_an_adapter_below_the_floor_is_not_pinned(
        self, tmp_path, agents_dir, monkeypatch, version
    ):
        """The pin is verified on 0.84.0 only, so an older adapter keeps today's behaviour."""
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        sent, killed = self._adapter_transport(
            client, monkeypatch, started_mode="bypassPermissions", version=version
        )
        await client._initialize_session()
        assert client._session_id == "s-new"
        assert [m for m, _ in sent if m == "session/set_mode"] == []
        assert killed == []

    def test_a_write_after_an_old_adapters_handshake_refuses_the_exclusion(
        self, tmp_path, agents_dir
    ):
        """A re-seed after the handshake reads the version it already has."""
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        self._project_owned_settings(tmp_path, {"permissions": {}})
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._agent_version = "0.83.0"
        client._agent_version_read = True
        client._write_claude_local_settings()
        assert client._claude_local_settings_excluded is False
        assert client._session_mcp_servers() == []

    @pytest.mark.asyncio
    async def test_the_recorded_plan_session_is_kept_as_it_started(
        self, tmp_path, agents_dir, monkeypatch
    ):
        """Pure replay: 0.84.0 started a project ``plan`` file's session in ``plan``.

        The project tier is out of ``settingSources``, yet the adapter still
        resolved the starting mode from it, so the pin reads ``plan`` back and
        sends nothing.
        """
        client = self._excluded_client(
            tmp_path, agents_dir, {"permissions": {"defaultMode": "plan"}}
        )
        sent, killed = self._adapter_transport(client, monkeypatch, started_mode=None)
        await client._initialize_session()
        assert [m for m, _ in sent if m == "session/set_mode"] == []
        assert killed == []

    @pytest.mark.asyncio
    async def test_the_recorded_auto_file_starts_in_default(
        self, tmp_path, agents_dir, monkeypatch
    ):
        """Pure replay: 0.84.0 filtered a project ``auto`` and started in ``default``."""
        assert (
            self._recorded_results("project-auto-filtered")["session/new"][0]["modes"][
                "currentModeId"
            ]
            == "default"
        )
        client = self._excluded_client(
            tmp_path, agents_dir, {"permissions": {"defaultMode": "auto"}}
        )
        sent, killed = self._adapter_transport(
            client, monkeypatch, started_mode=None, scenario="project-auto-filtered"
        )
        await client._initialize_session()
        assert [m for m, _ in sent if m == "session/set_mode"] == []
        assert killed == []

    @pytest.mark.asyncio
    async def test_the_recorded_set_mode_answer_completes_the_pin(
        self, tmp_path, agents_dir, monkeypatch
    ):
        """The pin succeeds on the answer 0.84.0 gave to ``set_mode`` ``default`` from ``auto``."""
        recorded = self._recorded_results("project-plan-then-auto-then-pin")
        assert recorded["session/set_mode"] == [{}, {}]
        client = self._excluded_client(tmp_path, agents_dir, {"permissions": {}})
        sent, killed = self._adapter_transport(client, monkeypatch, started_mode="auto")
        await client._initialize_session()
        assert [p for m, p in sent if m == "session/set_mode"] == [
            {"sessionId": "s-new", "modeId": "default"}
        ]
        assert killed == []

    @pytest.mark.asyncio
    async def test_a_crew_authored_session_is_not_pinned(self, tmp_path, agents_dir, monkeypatch):
        """The pin covers the exclusion path only.

        A user ``~/.claude`` mode reaching a session whose file Crew wrote is the
        inherited-config gap the spec names (W2-1), not something this change
        opens, so no ``session/set_mode`` is sent there.
        """
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "settings.json").write_text(
            json.dumps({"permissions": {"defaultMode": "bypassPermissions"}}), encoding="utf-8"
        )
        monkeypatch.setenv("HOME", str(home))
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        work = tmp_path / "work"
        work.mkdir()
        client = self._seeded(work, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        assert "permissions" not in json.loads(
            (work / ".claude" / "settings.local.json").read_text()
        )
        sent, killed = self._adapter_transport(
            client, monkeypatch, started_mode="bypassPermissions"
        )
        await client._initialize_session()
        assert client._session_id == "s-new"
        assert "foo" in _by_name(dict(sent)["session/new"]["mcpServers"])
        assert killed == []
        assert [p for m, p in sent if m == "session/set_mode"] == []

    @pytest.mark.asyncio
    async def test_a_crew_opt_in_mode_is_not_overridden(self, tmp_path, agents_dir, monkeypatch):
        """The documented ``auto`` opt-in keeps the mode Crew asked for."""
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(
            tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE, permission_mode="auto"
        )
        sent, killed = self._adapter_transport(client, monkeypatch, started_mode="auto")
        await client._initialize_session()
        assert client._session_id == "s-new"
        assert killed == []
        assert "session/set_mode" not in [m for m, _ in sent]

    @pytest.mark.asyncio
    async def test_a_session_without_crews_array_gains_no_pin(
        self, tmp_path, agents_dir, monkeypatch
    ):
        """No Crew tools, no pin: the adapter's own mode is left alone."""
        client = self._seeded(tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        monkeypatch.setattr(client, "_resolve_session_mcp_servers", lambda: [])
        sent, killed = self._adapter_transport(
            client, monkeypatch, started_mode="bypassPermissions"
        )
        await client._initialize_session()
        assert client._session_id == "s-new"
        assert killed == []
        assert "session/set_mode" not in [m for m, _ in sent]

    def test_both_session_establishment_paths_reach_the_pin(self):
        """session/new and a successful session/load each read back and pin."""
        import inspect

        source = inspect.getsource(client_mod.AcpClient._initialize_session)
        assert source.count("await self._pin_claude_starting_mode(") == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["new", "load"])
    async def test_a_side_read_only_session_starts_confined_and_pinned(
        self, tmp_path, agents_dir, monkeypatch, path
    ):
        """A Side Chat session gets only the read built-ins and no MCP server.

        The spec's servers are not mounted, every web call is forced to the host by
        an inline ``ask`` rule, settings hooks are off, bypass cannot be the
        starting mode, and an approving mode inherited from the user's settings is
        pinned to ``default`` before the first prompt -- on both establishment paths.
        """
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(
            tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE, side_read_only=True
        )
        if path == "load":
            client._resume_session_id = "s-old"
        sent, killed = self._adapter_transport(
            client, monkeypatch, started_mode="bypassPermissions"
        )

        await client._initialize_session()

        params = dict(sent)["session/load" if path == "load" else "session/new"]
        assert params["mcpServers"] == []
        options = params["_meta"]["claudeCode"]["options"]
        assert options["tools"] == ["Read", "Grep", "Glob", "WebFetch", "WebSearch"]
        assert options["strictMcpConfig"] is True
        assert options["allowDangerouslySkipPermissions"] is False
        assert "settingSources" not in options
        assert options["settings"]["permissions"]["ask"] == ["WebFetch", "WebSearch"]
        assert options["settings"]["disableAllHooks"] is True
        assert [p for m, p in sent if m == "session/set_mode"] == [
            {"sessionId": client._session_id, "modeId": "default"}
        ]
        assert killed == []

    def test_a_side_read_only_session_keeps_the_exclusions_restrictions(self, tmp_path, agents_dir):
        """On the exclusion path the side shape is added to, never swapped for,
        the inline settings: the project's deny rules still ride along and only
        the user tier loads."""
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        local = tmp_path / ".claude" / "settings.local.json"
        local.parent.mkdir(parents=True)
        local.write_text(
            json.dumps({"permissions": {"deny": ["Read(./secrets/**)"], "ask": ["WebFetch"]}}),
            encoding="utf-8",
        )
        client = AcpClient(
            work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE, side_read_only=True
        )
        client._claude_adapter_disk_version = "0.84.0"
        client._write_claude_local_settings()
        assert client._claude_local_settings_excluded is True

        options = client._claude_session_meta()["claudeCode"]["options"]

        assert options["settingSources"] == ["user"]
        permissions = options["settings"]["permissions"]
        assert "Read(./secrets/**)" in permissions["deny"]
        assert permissions["ask"] == ["WebFetch", "WebSearch"]
        assert options["tools"] == ["Read", "Grep", "Glob", "WebFetch", "WebSearch"]

    @pytest.mark.parametrize(("side_read_only", "builtin"), [(True, "fs_read"), (False, "")])
    def test_only_a_side_read_only_session_names_a_claude_read_builtin(
        self, tmp_path, side_read_only, builtin
    ):
        """The mapped name is how the READ_ONLY gate proves a claude read on a
        Side Chat session; on any other session it would bind kiro-cli-named deny
        rules to claude's own tools."""
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(
            work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE, side_read_only=side_read_only
        )
        client._extract_tool_event(
            JsonRpcMessage(
                method="session/update",
                params={
                    "update": {
                        "_meta": {"claudeCode": {"toolName": "Read"}},
                        "sessionUpdate": "tool_call",
                        "toolCallId": "toolu_1",
                        "title": "Read /etc/hosts",
                        "kind": "read",
                        "rawInput": {"file_path": "/etc/hosts"},
                    }
                },
            )
        )
        event = client._build_permission_event(
            JsonRpcMessage(
                id=7,
                method="session/request_permission",
                params={
                    "toolCall": {
                        "toolCallId": "toolu_1",
                        "title": "Read /etc/hosts",
                        "kind": "read",
                    },
                    "options": [
                        {"optionId": "allow-once", "name": "Yes", "kind": "allow_once"},
                        {"optionId": "reject-once", "name": "No", "kind": "reject_once"},
                    ],
                },
            )
        )
        assert event is not None
        assert event.harness_builtin_tool == builtin

    @pytest.mark.asyncio
    @pytest.mark.parametrize("version", ["0.83.0", None])
    async def test_a_handshake_below_the_floor_stops_a_side_read_only_session(
        self, tmp_path, agents_dir, monkeypatch, version
    ):
        """The read-only shape rides session options an unverified adapter may
        ignore, so the session never reaches its first prompt."""
        client = self._seeded(
            tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE, side_read_only=True
        )
        _sent, killed = self._adapter_transport(
            client, monkeypatch, started_mode="default", version=version
        )
        with pytest.raises(client_mod.AcpError, match="not verified to honour settingSources"):
            await client._initialize_session()
        assert killed == [True]

    @pytest.mark.parametrize(
        ("version", "expected"),
        [("0.84.0", True), ("0.86.0", True), ("0.81.2", False), ("", False)],
    )
    def test_side_chat_asks_the_installed_adapters_version(self, monkeypatch, version, expected):
        """Side Chat grants tools on claude only from an adapter at the floor."""
        from kiro_crew.agent_sdk.drivers.acp import claude_adapter_honours_session_options

        monkeypatch.setattr(client_mod, "_claude_acp_argv_cache", (["/x/claude-agent-acp"], ""))
        monkeypatch.setattr(client_mod, "_claude_adapter_installed_version", lambda argv: version)
        assert claude_adapter_honours_session_options() is expected

    def test_side_chat_sees_no_adapter_as_unverified(self, monkeypatch):
        from kiro_crew.agent_sdk.drivers.acp import claude_adapter_honours_session_options

        monkeypatch.setattr(client_mod, "_claude_acp_argv_cache", (None, "PATH"))
        assert claude_adapter_honours_session_options() is False

    @pytest.mark.parametrize(
        "raw",
        [
            "not json",
            "[]",
            json.dumps({"permissions": {"defaultMode": "bypassPermissions"}}),
        ],
    )
    def test_the_project_file_is_never_read_to_decide(self, tmp_path, agents_dir, raw):
        """Any regular project file is left out, whatever it holds."""
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        path = tmp_path / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True)
        path.write_text(raw, encoding="utf-8")
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._write_claude_local_settings()
        assert "foo" in _by_name(client._session_mcp_servers())
        assert client._claude_session_meta()["claudeCode"]["options"]["settingSources"] == ["user"]

    def test_a_requested_mode_still_withholds_the_array(self, tmp_path, agents_dir):
        """A mode Crew asked for cannot be pinned without the local file."""
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        self._project_owned_settings(tmp_path, {"permissions": {"allow": ["mcp__foo__write"]}})
        client = AcpClient(
            work_dir=tmp_path,
            agent="kirocrew",
            acp_backend=ACP_BACKEND_CLAUDE,
            permission_mode="auto",
        )
        client._write_claude_local_settings()
        assert client._session_mcp_servers() == []
        assert client._claude_session_meta() == {"claudeCode": {"options": {}}}

    def test_a_symlinked_project_file_still_withholds_the_array(self, tmp_path, agents_dir):
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        target = tmp_path / "elsewhere.json"
        target.write_text(json.dumps({"permissions": {}}), encoding="utf-8")
        path = tmp_path / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True)
        path.symlink_to(target)
        client = AcpClient(work_dir=tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._write_claude_local_settings()
        assert client._session_mcp_servers() == []
        assert client._claude_session_meta() == {"claudeCode": {"options": {}}}

    def test_the_cached_array_is_not_aliased_to_callers(self, tmp_path, agents_dir):
        # The two call sites splat this list into their params; handing out the
        # cache itself would let one session/load mutation reach the next.
        _write_spec(agents_dir, servers={"foo": {"command": "/bin/foo"}}, tools=["@foo"])
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        first = client._session_mcp_servers()
        first.clear()
        assert "foo" in _by_name(client._session_mcp_servers())

    def test_a_pooled_stub_the_spec_never_references_does_not_mount(self, tmp_path, agents_dir):
        """The `tools` allowlist covers the pooled half of the array too.

        The gateway rewriter writes each agent's overlay from the GLOBAL settings
        file as well as the agent's own spec, so the overlay can carry a stub for
        a server this agent's ``tools`` never references -- and a stub is that
        server. Before the mirror placed the stubs, the shared append mounted it
        anyway, so a claude session received a pooled server the same spec would
        NOT mount on kiro-cli or codex. The unreferenced server is the widening
        direction, and this is the parity this test pins.
        """
        _write_spec(agents_dir, servers={"direct": {"command": "/bin/direct"}}, tools=["@direct"])
        client = self._seeded(tmp_path, agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE)
        client._pooled_broker_stubs = lambda: [  # type: ignore[method-assign]
            {"name": "unreferenced", "command": "/stub", "args": [], "env": [], "type": "stdio"},
        ]
        names = set(_by_name(client._session_mcp_servers()))
        assert "direct" in names
        assert "unreferenced" not in names
        # And the shared append is inert for claude, so nothing re-adds it later.
        assert client._pooled_mcp_servers() == []

    def test_the_shared_append_still_pools_for_kiro(self, tmp_path):
        """The mirror-less backend keeps the shared append.

        kiro-cli reads the agent spec itself via ``--agent``, so the injected
        array is the ONLY channel its broker stubs arrive on -- the injection
        outranking the same-named spec entry is what pools them. Making the
        append inert for every MIRRORED backend must not take kiro's away.
        """
        client = AcpClient(work_dir=tmp_path, agent="kirocrew")
        stub = {"name": "pooled", "command": "/stub", "args": [], "env": [], "type": "stdio"}
        client._pooled_broker_stubs = lambda: [dict(stub)]  # type: ignore[method-assign]
        assert client._pooled_mcp_servers() == [stub]

    def _overlay_with_a_stub(self, root: Path) -> Path:
        """A user-level overlay holding one broker stub for ``kirocrew``."""
        from kiro_crew.mcp_gateway.rewriter import _WRAPPER_MARKER

        overlay = root / "mcp-gateway" / "agents"
        overlay.mkdir(parents=True)
        (overlay / "kirocrew.json").write_text(
            json.dumps(
                {
                    "name": "kirocrew",
                    "mcpServers": {
                        "pooled": {
                            _WRAPPER_MARKER: True,
                            "command": "/stub",
                            "args": ["--target-command=user-level-cmd"],
                            "env": {},
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        return overlay

    def test_a_project_agent_takes_no_stub_from_the_user_level_overlay(self, tmp_path, agents_dir):
        """The client's own checkout reaches the overlay lookup.

        Drives the real call chain rather than the module function, so a call site
        that stops passing ``work_dir`` -- or is wrapped in a guard that is never
        true -- fails here instead of silently resolving the overlay by name.
        """
        overlay = self._overlay_with_a_stub(tmp_path / "gw")
        checkout = tmp_path / "checkout"
        _write_project_spec(checkout, servers={"proj": {"command": "/bin/proj"}}, tools=None)

        running_the_project_agent = AcpClient(work_dir=checkout, agent="kirocrew")
        running_the_project_agent._mcp_gateway_overlay = overlay
        assert running_the_project_agent._pooled_broker_stubs() == []

        # Control: the same overlay and agent name, a checkout that declares
        # neither, so the user-level stub is still this session's.
        elsewhere = AcpClient(work_dir=tmp_path / "plain", agent="kirocrew")
        elsewhere._mcp_gateway_overlay = overlay
        assert [e["name"] for e in elsewhere._pooled_broker_stubs()] == ["pooled"]


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

    def test_seed_never_writes_a_model_without_the_list_it_must_match(self, tmp_path, monkeypatch):
        # The exact shape observed in the field: "model": "claude-opus-5" beside an
        # allowlist that contains no Opus 5 entry, which resolves to 200K. The two
        # keys are now written together or not at all.
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
