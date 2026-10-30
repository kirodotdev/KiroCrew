"""Tests for kiro_crew.apps.permissions — permission validation and enforcement."""

from __future__ import annotations

from kiro_crew.apps.manager import InstalledApp
from kiro_crew.apps.manifest import AppManifest, Permissions
from kiro_crew.apps.permissions import (
    PermissionCheck,
    app_can_manage_session_approvals,
    check_tool_permission,
    format_permissions_summary,
    validate_permissions,
)


class TestValidatePermissions:
    def test_clean_manifest(self):
        m = AppManifest(
            name="test-app",
            version="1.0.0",
            displayName="Test",
            description="Test app",
            permissions=Permissions(mcpTools=["ToolA"], storage=True),
        )
        result = validate_permissions(m)
        assert result.allowed is True
        assert result.denied == []

    def test_many_tools_warning(self):
        m = AppManifest(
            name="test-app",
            version="1.0.0",
            displayName="Test",
            description="Test app",
            permissions=Permissions(mcpTools=[f"Tool{i}" for i in range(15)]),
        )
        result = validate_permissions(m)
        assert result.allowed is True
        assert any("15 MCP tools" in w for w in result.warnings)

    def test_network_warning(self):
        m = AppManifest(
            name="test-app",
            version="1.0.0",
            displayName="Test",
            description="Test app",
            permissions=Permissions(network=True),
        )
        result = validate_permissions(m)
        assert any("network" in w.lower() for w in result.warnings)

    def test_shared_memory_warning(self):
        m = AppManifest(
            name="test-app",
            version="1.0.0",
            displayName="Test",
            description="Test app",
            permissions=Permissions(memory="shared"),
        )
        result = validate_permissions(m)
        assert any("shared memory" in w.lower() for w in result.warnings)

    def test_path_traversal_denied(self):
        m = AppManifest(
            name="test-app",
            version="1.0.0",
            displayName="Test",
            description="Test app",
            agents=["../evil.json"],
        )
        result = validate_permissions(m)
        assert result.allowed is False
        assert len(result.denied) > 0


class TestCheckToolPermission:
    def test_allowed_tool(self):
        m = AppManifest(
            name="test",
            version="1.0.0",
            displayName="T",
            description="T",
            permissions=Permissions(mcpTools=["ToolA", "ToolB"]),
        )
        assert check_tool_permission("test", "ToolA", m) is True

    def test_denied_tool(self):
        m = AppManifest(
            name="test",
            version="1.0.0",
            displayName="T",
            description="T",
            permissions=Permissions(mcpTools=["ToolA"]),
        )
        assert check_tool_permission("test", "ToolC", m) is False

    def test_empty_tools_unrestricted(self):
        m = AppManifest(
            name="test",
            version="1.0.0",
            displayName="T",
            description="T",
            permissions=Permissions(mcpTools=[]),
        )
        assert check_tool_permission("test", "AnyTool", m) is True


class TestManagedToolsToExclude:
    """``permissions.mcpTools`` is enforced by excluding the managed tools an app
    did NOT declare from its agents' ``managedToolPolicy``.

    The set is "every host-managed tool that fails :func:`check_tool_permission`",
    so the predicate the issue asked to wire up is the one that decides the
    exclusion. An app's own server tools and base built-ins are never managed
    tools, so they are never excluded; an empty declaration excludes nothing.
    """

    def test_empty_declaration_excludes_nothing(self):
        from kiro_crew.apps.permissions import managed_tools_to_exclude

        m = AppManifest(
            name="app",
            version="1.0.0",
            displayName="A",
            description="A",
            permissions=Permissions(mcpTools=[]),
        )
        assert managed_tools_to_exclude(m) == []

    def test_a_declared_managed_tool_is_not_excluded(self):
        from kiro_crew.apps.permissions import managed_tools_to_exclude

        m = AppManifest(
            name="app",
            version="1.0.0",
            displayName="A",
            description="A",
            permissions=Permissions(mcpTools=["cron_add", "cron_list"]),
        )
        excl = managed_tools_to_exclude(m)
        assert "cron_add" not in excl
        assert "cron_list" not in excl

    def test_an_undeclared_managed_tool_is_excluded(self):
        from kiro_crew.apps.permissions import managed_tools_to_exclude

        m = AppManifest(
            name="app",
            version="1.0.0",
            displayName="A",
            description="A",
            permissions=Permissions(mcpTools=["cron_add"]),
        )
        excl = managed_tools_to_exclude(m)
        # A managed tool the app never asked for is withheld.
        assert "spawn_run" in excl
        assert "cron_remove" in excl

    def test_the_apps_own_server_tool_is_never_excluded(self):
        """A ``@<server>/tool`` ref the app declared is not a managed tool, and
        declaring one does not pull any managed tool into the allow-list — so the
        whole managed universe is excluded, and the app's own ref is not in it."""
        from kiro_crew.apps.permissions import (
            _FRAMEWORK_REQUIRED_MANAGED_TOOLS,
            _managed_tool_universe,
            managed_tools_to_exclude,
        )

        m = AppManifest(
            name="app",
            version="1.0.0",
            displayName="A",
            description="A",
            permissions=Permissions(mcpTools=["@app:do_thing"]),
        )
        excl = set(managed_tools_to_exclude(m))
        assert "@app:do_thing" not in excl
        # Nothing managed was declared, so every managed tool is excluded except
        # the ones the framework itself needs on every app agent.
        assert excl == _managed_tool_universe() - _FRAMEWORK_REQUIRED_MANAGED_TOOLS

    def test_the_result_is_sorted_and_deduplicated(self):
        from kiro_crew.apps.permissions import managed_tools_to_exclude

        m = AppManifest(
            name="app",
            version="1.0.0",
            displayName="A",
            description="A",
            permissions=Permissions(mcpTools=["cron_add"]),
        )
        excl = managed_tools_to_exclude(m)
        assert excl == sorted(excl)
        assert len(excl) == len(set(excl))


class TestManagedToolUniverse:
    """The universe is the AUTHORITATIVE managed-tool set — each managed server's
    own ``_list_tools()`` read in-process — not the display-title table, which
    tables only three of the ten managed servers.
    """

    def test_it_unions_every_managed_server(self):
        from kiro_crew.apps.permissions import _managed_tool_universe

        names = _managed_tool_universe()
        # A sample from servers the display-title table does NOT cover, so this
        # fails if the universe regresses to the title table (which tabled only
        # core / cron / dashboard and missed work / debug / panel / guide / mate).
        assert "spawn_run" in names  # kirocrew-core
        assert "cron_add" in names  # kirocrew-cron
        assert "session_create" in names  # kirocrew-dashboard

    def test_it_covers_servers_the_title_table_omits(self):
        """The regression the First Principles lane flagged: a second, incomplete
        source. Every managed server in the registry must contribute, including the
        seven the title table never listed."""
        from kiro_crew.apps.permissions import _managed_tool_universe
        from kiro_crew.mcp_discovery import (
            _MANAGED_SERVER_TOOL_MODULES,
            _managed_tools_in_process,
        )

        names = _managed_tool_universe()
        for server in _MANAGED_SERVER_TOOL_MODULES:
            tools = _managed_tools_in_process(server)
            if tools:  # a server with tools must be fully represented
                assert set(tools) <= names, f"{server} tools missing from the universe"

    def test_feature_gated_tools_are_in_the_universe_while_disabled(self, monkeypatch):
        """The GPT security finding: ``kirocrew-computer`` hides its tools behind
        the computer-use enable, so the discovery read returns ``[]`` while it is
        off (the default). The policy universe must still carry every
        ``computer_*`` name — otherwise enabling computer use later (which does not
        re-materialize the app agent) would expose undeclared desktop actions.
        Force the feature OFF and assert the tools are present anyway."""
        import kiro_crew.mcp_computer as mcp_computer
        from kiro_crew.apps.permissions import _managed_tool_universe

        # Computer use OFF -> the gated _list_tools() returns [] ...
        monkeypatch.setattr(mcp_computer.enable_state, "is_enabled", lambda: False)
        assert mcp_computer._list_tools() == []
        # ... yet the policy universe still names its mutating tools.
        names = _managed_tool_universe()
        assert "computer_click" in names
        assert "computer_get_state" in names

    def test_a_scoped_app_excludes_disabled_computer_tools(self, monkeypatch):
        """End of the same finding: an app that declares only the observation tool
        still has every mutating computer tool excluded, even with computer use
        off at materialization time."""
        import kiro_crew.mcp_computer as mcp_computer
        from kiro_crew.apps.permissions import managed_tools_to_exclude

        monkeypatch.setattr(mcp_computer.enable_state, "is_enabled", lambda: False)
        m = AppManifest(
            name="watcher",
            version="1.0.0",
            displayName="W",
            description="W",
            permissions=Permissions(mcpTools=["computer_get_state"]),
        )
        excl = set(managed_tools_to_exclude(m))
        assert "computer_click" in excl, "a disabled mutating tool must still be excluded"
        assert "computer_get_state" not in excl, "the declared tool stays reachable"

    def test_base_builtins_are_not_managed_tools(self):
        """``read`` / ``glob`` / ``grep`` are kiro-cli native, not host-managed, so
        enforcement never touches an agent's ability to read its own files."""
        from kiro_crew.apps.permissions import _managed_tool_universe

        names = _managed_tool_universe()
        for builtin in ("read", "glob", "grep", "web_fetch", "web_search"):
            assert builtin not in names


class TestUnreadableManagedServer:
    """A managed server whose tool declaration reads ``None`` has unknown names, so
    a name-based exclusion cannot cover it. It is withheld whole instead."""

    @staticmethod
    def _unreadable(monkeypatch, server):
        import kiro_crew.mcp_discovery_managed as managed

        real = managed.managed_tool_names_for_policy
        monkeypatch.setattr(
            managed,
            "managed_tool_names_for_policy",
            lambda name: None if name == server else real(name),
        )

    def test_the_server_is_returned_to_withhold(self, monkeypatch):
        from kiro_crew.apps.permissions import managed_tool_exclusion

        self._unreadable(monkeypatch, "kirocrew-cron")
        m = AppManifest(
            name="app",
            version="1.0.0",
            displayName="A",
            description="A",
            permissions=Permissions(mcpTools=["spawn_run"]),
        )
        out = managed_tool_exclusion(m)
        assert out.servers == ["kirocrew-cron"]
        assert "spawn_run" not in out.tools
        assert "session_create" in out.tools, "readable servers are still excluded by name"

    def test_an_unrestricted_app_withholds_nothing(self, monkeypatch):
        from kiro_crew.apps.permissions import managed_tool_exclusion

        self._unreadable(monkeypatch, "kirocrew-cron")
        m = AppManifest(
            name="app",
            version="1.0.0",
            displayName="A",
            description="A",
            permissions=Permissions(mcpTools=[]),
        )
        assert managed_tool_exclusion(m) == ([], [])

    def test_every_server_readable_withholds_no_server(self):
        from kiro_crew.apps.permissions import managed_tool_exclusion

        m = AppManifest(
            name="app",
            version="1.0.0",
            displayName="A",
            description="A",
            permissions=Permissions(mcpTools=["cron_add"]),
        )
        assert managed_tool_exclusion(m).servers == []


class TestFormatSummary:
    def test_with_permissions(self):
        m = AppManifest(
            name="test",
            version="1.0.0",
            displayName="T",
            description="T",
            permissions=Permissions(
                mcpTools=["ToolA", "ToolB"],
                storage=True,
                cron=True,
            ),
        )
        summary = format_permissions_summary(m)
        assert "ToolA" in summary
        assert "Storage" in summary
        assert "Cron" in summary

    def test_no_permissions(self):
        m = AppManifest(
            name="test",
            version="1.0.0",
            displayName="T",
            description="T",
        )
        summary = format_permissions_summary(m)
        assert "No special permissions" in summary


class TestSessionApprovalPermission:
    def test_enabled_app_with_grant_is_allowed(self, monkeypatch):
        manifest = AppManifest(
            name="test",
            version="1.0.0",
            displayName="T",
            description="T",
            permissions=Permissions(sessionApproval=True),
        )
        monkeypatch.setattr(
            "kiro_crew.apps.permissions.is_app_enabled", lambda name: name == "test"
        )
        monkeypatch.setattr("kiro_crew.apps.permissions.get_app_manifest", lambda _name: manifest)
        monkeypatch.setattr(
            "kiro_crew.apps.permissions._read_installed",
            lambda name, **_kw: InstalledApp(name=name),
        )

        assert app_can_manage_session_approvals("test") is True

    def test_unreadable_record_is_denied_even_when_enabled_reads_true(self, monkeypatch):
        manifest = AppManifest(
            name="test",
            version="1.0.0",
            displayName="T",
            description="T",
            permissions=Permissions(sessionApproval=True),
        )
        monkeypatch.setattr("kiro_crew.apps.permissions.is_app_enabled", lambda _name: True)
        monkeypatch.setattr("kiro_crew.apps.permissions.get_app_manifest", lambda _name: manifest)
        monkeypatch.setattr("kiro_crew.apps.permissions._read_installed", lambda _name, **_kw: None)

        assert app_can_manage_session_approvals("test") is False

    def test_disabled_app_is_denied(self, monkeypatch):
        monkeypatch.setattr("kiro_crew.apps.permissions.is_app_enabled", lambda _name: False)
        assert app_can_manage_session_approvals("test") is False

    def test_missing_grant_is_denied(self, monkeypatch):
        manifest = AppManifest(
            name="test",
            version="1.0.0",
            displayName="T",
            description="T",
        )
        monkeypatch.setattr("kiro_crew.apps.permissions.is_app_enabled", lambda _name: True)
        monkeypatch.setattr("kiro_crew.apps.permissions.get_app_manifest", lambda _name: manifest)

        assert app_can_manage_session_approvals("test") is False


class TestPermissionCheck:
    def test_to_dict(self):
        pc = PermissionCheck(allowed=True, warnings=["w1"], denied=[])
        d = pc.to_dict()
        assert d["allowed"] is True
        assert d["warnings"] == ["w1"]
        assert "denied" not in d  # empty list omitted
