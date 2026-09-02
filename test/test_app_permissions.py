"""Tests for kiro_crew.apps.permissions — permission validation and enforcement."""
from __future__ import annotations

from kiro_crew.apps.manifest import AppManifest, Permissions
from kiro_crew.apps.permissions import (
    PermissionCheck,
    check_tool_permission,
    format_permissions_summary,
    validate_permissions,
)


class TestValidatePermissions:
    def test_clean_manifest(self):
        m = AppManifest(
            name="test-app", version="1.0.0", displayName="Test",
            description="Test app",
            permissions=Permissions(mcpTools=["ToolA"], storage=True),
        )
        result = validate_permissions(m)
        assert result.allowed is True
        assert result.denied == []

    def test_many_tools_warning(self):
        m = AppManifest(
            name="test-app", version="1.0.0", displayName="Test",
            description="Test app",
            permissions=Permissions(mcpTools=[f"Tool{i}" for i in range(15)]),
        )
        result = validate_permissions(m)
        assert result.allowed is True
        assert any("15 MCP tools" in w for w in result.warnings)

    def test_network_warning(self):
        m = AppManifest(
            name="test-app", version="1.0.0", displayName="Test",
            description="Test app",
            permissions=Permissions(network=True),
        )
        result = validate_permissions(m)
        assert any("network" in w.lower() for w in result.warnings)

    def test_shared_memory_warning(self):
        m = AppManifest(
            name="test-app", version="1.0.0", displayName="Test",
            description="Test app",
            permissions=Permissions(memory="shared"),
        )
        result = validate_permissions(m)
        assert any("shared memory" in w.lower() for w in result.warnings)

    def test_path_traversal_denied(self):
        m = AppManifest(
            name="test-app", version="1.0.0", displayName="Test",
            description="Test app",
            agents=["../evil.json"],
        )
        result = validate_permissions(m)
        assert result.allowed is False
        assert len(result.denied) > 0


class TestCheckToolPermission:
    def test_allowed_tool(self):
        m = AppManifest(
            name="test", version="1.0.0", displayName="T", description="T",
            permissions=Permissions(mcpTools=["ToolA", "ToolB"]),
        )
        assert check_tool_permission("test", "ToolA", m) is True

    def test_denied_tool(self):
        m = AppManifest(
            name="test", version="1.0.0", displayName="T", description="T",
            permissions=Permissions(mcpTools=["ToolA"]),
        )
        assert check_tool_permission("test", "ToolC", m) is False

    def test_empty_tools_unrestricted(self):
        m = AppManifest(
            name="test", version="1.0.0", displayName="T", description="T",
            permissions=Permissions(mcpTools=[]),
        )
        assert check_tool_permission("test", "AnyTool", m) is True


class TestFormatSummary:
    def test_with_permissions(self):
        m = AppManifest(
            name="test", version="1.0.0", displayName="T", description="T",
            permissions=Permissions(
                mcpTools=["ToolA", "ToolB"], storage=True, cron=True,
            ),
        )
        summary = format_permissions_summary(m)
        assert "ToolA" in summary
        assert "Storage" in summary
        assert "Cron" in summary

    def test_no_permissions(self):
        m = AppManifest(
            name="test", version="1.0.0", displayName="T", description="T",
        )
        summary = format_permissions_summary(m)
        assert "No special permissions" in summary


class TestPermissionCheck:
    def test_to_dict(self):
        pc = PermissionCheck(allowed=True, warnings=["w1"], denied=[])
        d = pc.to_dict()
        assert d["allowed"] is True
        assert d["warnings"] == ["w1"]
        assert "denied" not in d  # empty list omitted
