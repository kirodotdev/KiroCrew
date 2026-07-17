"""Tests for kiro_crew.apps.dependencies — dependency resolution."""
from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from kiro_crew.apps.dependencies import (
    DependencyResult,
    _get_dep_id,
    _get_managed_by,
    resolve_dependencies,
)
from kiro_crew.apps.manifest import AimDependencies, Dependencies


@pytest.fixture(autouse=True)
def _dep_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "kirocrew-home"))


class TestHelpers:
    def test_get_dep_id_string(self):
        assert _get_dep_id("aws-docs") == "aws-docs"

    def test_get_dep_id_dict(self):
        assert _get_dep_id({"id": "custom", "managedBy": "app"}) == "custom"

    def test_get_managed_by_string(self):
        assert _get_managed_by("x", "gateway") == "gateway"

    def test_get_managed_by_dict_override(self):
        assert _get_managed_by({"id": "x", "managedBy": "app"}, "gateway") == "app"

    def test_get_managed_by_dict_default(self):
        assert _get_managed_by({"id": "x"}, "gateway") == "gateway"


class TestDependencyResult:
    def test_to_dict_empty(self):
        assert DependencyResult().to_dict() == {}

    def test_to_dict_populated(self):
        r = DependencyResult(installed=["a"], failed=["b"], missing=["node"])
        d = r.to_dict()
        assert d["installed"] == ["a"]
        assert d["failed"] == ["b"]
        assert d["missing"] == ["node"]


@pytest.mark.asyncio
class TestResolveDependencies:
    async def test_commands_check(self):
        """Commands that exist are skipped, missing ones go to missing list."""
        deps = Dependencies(commands=["sh", "nonexistent-cmd-xyz"])
        result = await resolve_dependencies("test-app", deps)
        # sh should exist on any unix system
        assert "command:sh" in result.skipped
        assert "nonexistent-cmd-xyz" in result.missing

    async def test_app_managed_deps_skipped(self):
        """managedBy=app deps are skipped without calling aim."""
        deps = Dependencies(
            managedBy="app",
            aim=AimDependencies(mcp=["some-mcp"]),
        )
        result = await resolve_dependencies("test-app", deps)
        assert "aim/mcp/some-mcp" in result.skipped
        assert result.installed == []

    async def test_empty_deps(self):
        deps = Dependencies()
        result = await resolve_dependencies("test-app", deps)
        assert result.to_dict() == {}


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------

# Commands that definitely exist on any unix system
_EXISTING_CMDS = ["sh", "ls", "cat", "echo"]
_NONEXISTENT_CMDS = ["zzz-no-such-cmd-1", "zzz-no-such-cmd-2", "zzz-no-such-cmd-3"]


class TestDependencyProperties:
    # Feature: app-classification-redesign, Property 5: 缺失命令检测
    @given(
        existing=st.lists(st.sampled_from(_EXISTING_CMDS), max_size=4, unique=True),
        missing=st.lists(st.sampled_from(_NONEXISTENT_CMDS), max_size=3, unique=True),
    )
    @settings(max_examples=100)
    def test_missing_command_detection(self, existing, missing):
        """**Validates: Requirements 5.7**"""
        import asyncio
        deps = Dependencies(commands=existing + missing)
        result = asyncio.run(
            resolve_dependencies("test-app", deps)
        )
        for cmd in existing:
            assert cmd not in result.missing
            assert f"command:{cmd}" in result.skipped
        for cmd in missing:
            assert cmd in result.missing
