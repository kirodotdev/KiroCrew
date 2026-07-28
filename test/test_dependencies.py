"""Tests for kiro_crew.apps.dependencies — dependency resolution."""
from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from kiro_crew.apps.dependencies import (
    DependencyResult,
    _get_dep_id,
    _get_managed_by,
    clean_dependencies,
    resolve_dependencies,
)
from kiro_crew.apps.manifest import CapabilityDependencies, Dependencies
from kiro_crew.platform.interfaces import CapabilityResult


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
        """managedBy=app deps are skipped without touching the capability seam."""
        deps = Dependencies(
            managedBy="app",
            capabilities=CapabilityDependencies(mcp=["some-mcp"]),
        )
        result = await resolve_dependencies("test-app", deps)
        assert "capability/mcp/some-mcp" in result.skipped
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


# ---------------------------------------------------------------------------
# Capability-seam resolution (replaces the former external-CLI shell-out)
# ---------------------------------------------------------------------------


class _FakeManager:
    """CapabilityManager stand-in recording the ops it received.

    Implements the FULL Protocol (not just the ops this module calls) and returns
    the REAL ``CapabilityResult`` — a fake that drifts from
    ``platform/interfaces.py`` can keep these tests green while production breaks,
    and the Protocol is not ``runtime_checkable`` so nothing else would notice.
    """

    def __init__(self, *, available: bool = True, ok: bool = True) -> None:
        self._available = available
        self._ok = ok
        self.calls: list[tuple[str, str]] = []

    def _result(self) -> CapabilityResult:
        return CapabilityResult(ok=self._ok, message="" if self._ok else "boom")

    def available(self) -> bool:
        return self._available

    # -- read ops (unused by the resolver, present for Protocol fidelity) --
    async def list_mcp(self) -> list[dict]:
        return []

    async def registry(self) -> list[dict]:
        return []

    async def list_skills(self) -> list[dict]:
        return []

    async def list_agents(self) -> list[dict]:
        return []

    async def install_mcp(self, server_id: str):
        self.calls.append(("install_mcp", server_id))
        return self._result()

    async def install_skill(self, package: str):
        self.calls.append(("install_skill", package))
        return self._result()

    async def uninstall_mcp(self, server_id: str):
        self.calls.append(("uninstall_mcp", server_id))
        return self._result()

    async def uninstall_skill(self, package: str):
        self.calls.append(("uninstall_skill", package))
        return self._result()


@pytest.fixture
def fake_manager(monkeypatch):
    """Install a fake capability manager into the resolver's seam lookup."""
    def _install(**kwargs):
        mgr = _FakeManager(**kwargs)
        monkeypatch.setattr(
            "kiro_crew.apps.dependencies._capability_manager",
            lambda: mgr if mgr.available() else None,
        )
        return mgr
    return _install


@pytest.mark.asyncio
class TestCapabilitySeamResolution:
    async def test_installs_mcp_and_skills_through_seam(self, fake_manager):
        mgr = fake_manager()
        deps = Dependencies(
            capabilities=CapabilityDependencies(mcp=["some-mcp"], skills=["SomeSkill"]),
        )
        result = await resolve_dependencies("test-app", deps)
        assert mgr.calls == [("install_mcp", "some-mcp"), ("install_skill", "SomeSkill")]
        assert result.installed == ["capability/mcp/some-mcp", "capability/skills/SomeSkill"]
        assert result.failed == []

    async def test_unavailable_manager_records_failure_not_crash(self, fake_manager):
        """The public edition has no capability manager: the app still installs,
        but the unmet dep must be reported rather than silently 'succeeding'."""
        fake_manager(available=False)
        deps = Dependencies(capabilities=CapabilityDependencies(mcp=["some-mcp"]))
        result = await resolve_dependencies("test-app", deps)
        assert result.failed == ["capability/mcp/some-mcp"]
        assert result.installed == []

    async def test_failed_op_is_recorded(self, fake_manager):
        fake_manager(ok=False)
        deps = Dependencies(capabilities=CapabilityDependencies(mcp=["some-mcp"]))
        result = await resolve_dependencies("test-app", deps)
        assert result.failed == ["capability/mcp/some-mcp"]

    async def test_agents_have_no_install_op(self, fake_manager):
        """``agents`` is declarable but the seam exposes no install op — it must
        report as unresolved instead of being dropped on the floor."""
        mgr = fake_manager()
        deps = Dependencies(capabilities=CapabilityDependencies(agents=["SomePkg"]))
        result = await resolve_dependencies("test-app", deps)
        assert result.failed == ["capability/agents/SomePkg"]
        assert mgr.calls == []

    async def test_no_seam_probe_when_nothing_to_install(self, monkeypatch):
        """Commands-only manifests must not touch the capability seam at all."""
        probed = []
        monkeypatch.setattr(
            "kiro_crew.apps.dependencies._capability_manager",
            lambda: probed.append(1) or None,
        )
        await resolve_dependencies("test-app", Dependencies(commands=["sh"]))
        assert probed == []

    async def test_clean_dependencies_uninstalls_through_seam(self, fake_manager):
        mgr = fake_manager()
        cleaned = await clean_dependencies(
            "test-app",
            [{"id": "capability/mcp/some-mcp", "type": "capability.mcp"}],
        )
        assert mgr.calls == [("uninstall_mcp", "some-mcp")]
        assert cleaned == ["capability/mcp/some-mcp"]

    async def test_clean_dependencies_accepts_legacy_type(self, fake_manager):
        """A ledger row written before the rename still carries ``aim.mcp``; its
        cleanup must still dispatch, or the dep leaks forever."""
        mgr = fake_manager()
        cleaned = await clean_dependencies(
            "test-app", [{"id": "capability/mcp/old", "type": "aim.mcp"}],
        )
        assert mgr.calls == [("uninstall_mcp", "old")]
        assert cleaned == ["capability/mcp/old"]

    async def test_clean_skips_unknown_type(self, fake_manager):
        mgr = fake_manager()
        cleaned = await clean_dependencies(
            "test-app", [{"id": "capability/bogus/x", "type": "capability.bogus"}],
        )
        assert mgr.calls == []
        assert cleaned == []


class TestDeprecatedManifestAlias:
    def test_aim_alias_still_loads(self):
        """Manifests authored against the pre-rename schema must keep working."""
        deps = Dependencies.from_dict({"aim": {"mcp": ["legacy-mcp"]}})
        assert deps.capabilities.mcp == ["legacy-mcp"]

    def test_alias_is_not_re_emitted(self):
        """Round-tripping migrates the manifest to the canonical key."""
        d = Dependencies.from_dict({"aim": {"mcp": ["legacy-mcp"]}}).to_dict()
        assert "aim" not in d
        assert d["capabilities"] == {"mcp": ["legacy-mcp"]}

    def test_canonical_key_wins_over_alias(self):
        deps = Dependencies.from_dict({
            "capabilities": {"mcp": ["new"]}, "aim": {"mcp": ["old"]},
        })
        assert deps.capabilities.mcp == ["new"]


class TestCapabilityManagerAccessor:
    """Covers the REAL ``_capability_manager()`` body (the seam read itself).

    The seam-resolution tests above replace this accessor wholesale, so without
    these the ``current_context()`` read, the ``available()`` gate, and both
    fail-closed handlers would have no coverage at all — dropping the
    ``available()`` gate entirely would still leave that suite green.
    """

    def test_reads_context_and_preserves_bound(self, monkeypatch):
        """Returns the context-provided manager as-is, so the
        ``BoundedCapabilityManager`` timeout wrapper applied at context
        composition is inherited rather than stripped."""
        from kiro_crew.apps import dependencies as deps_mod
        from kiro_crew.platform import context as platform_context
        from kiro_crew.platform.capability_bound import BoundedCapabilityManager

        class _Inner:
            def available(self) -> bool:
                return True

        sentinel = BoundedCapabilityManager(_Inner())

        class _Ctx:
            capability_manager = sentinel

        monkeypatch.setattr(platform_context, "current_context", lambda: _Ctx())
        assert deps_mod._capability_manager() is sentinel

    def test_unavailable_manager_yields_none(self, monkeypatch):
        from kiro_crew.apps import dependencies as deps_mod
        from kiro_crew.platform import context as platform_context

        class _Unavailable:
            def available(self) -> bool:
                return False

        class _Ctx:
            capability_manager = _Unavailable()

        monkeypatch.setattr(platform_context, "current_context", lambda: _Ctx())
        assert deps_mod._capability_manager() is None

    def test_context_lookup_failure_fails_closed(self, monkeypatch):
        """A broken context must degrade to "unavailable", never propagate."""
        from kiro_crew.apps import dependencies as deps_mod
        from kiro_crew.platform import context as platform_context

        def _boom():
            raise RuntimeError("no context")

        monkeypatch.setattr(platform_context, "current_context", _boom)
        assert deps_mod._capability_manager() is None

    def test_available_probe_raising_fails_closed(self, monkeypatch):
        from kiro_crew.apps import dependencies as deps_mod
        from kiro_crew.platform import context as platform_context

        class _Raising:
            def available(self) -> bool:
                raise RuntimeError("probe exploded")

        class _Ctx:
            capability_manager = _Raising()

        monkeypatch.setattr(platform_context, "current_context", lambda: _Ctx())
        assert deps_mod._capability_manager() is None


@pytest.mark.asyncio
class TestCleanDependenciesFailurePaths:
    """``clean_dependencies``' failure branches — the resolve side has these
    covered, the cleanup side had none."""

    async def test_unavailable_manager_cleans_nothing(self, monkeypatch):
        monkeypatch.setattr("kiro_crew.apps.dependencies._capability_manager", lambda: None)
        cleaned = await clean_dependencies(
            "test-app", [{"id": "capability/mcp/x", "type": "capability.mcp"}],
        )
        assert cleaned == []

    async def test_failed_uninstall_is_not_reported_clean(self, fake_manager):
        fake_manager(ok=False)
        cleaned = await clean_dependencies(
            "test-app", [{"id": "capability/mcp/x", "type": "capability.mcp"}],
        )
        assert cleaned == []

    async def test_raising_op_is_swallowed(self, monkeypatch):
        class _Exploding(_FakeManager):
            async def uninstall_mcp(self, server_id: str):
                raise RuntimeError("boom")

        mgr = _Exploding()
        monkeypatch.setattr("kiro_crew.apps.dependencies._capability_manager", lambda: mgr)
        cleaned = await clean_dependencies(
            "test-app", [{"id": "capability/mcp/x", "type": "capability.mcp"}],
        )
        assert cleaned == []

    async def test_skill_uninstall_dispatches(self, fake_manager):
        mgr = fake_manager()
        cleaned = await clean_dependencies(
            "test-app", [{"id": "capability/skills/Pkg", "type": "capability.skills"}],
        )
        assert mgr.calls == [("uninstall_skill", "Pkg")]
        assert cleaned == ["capability/skills/Pkg"]

    async def test_blank_id_is_skipped(self, fake_manager):
        mgr = fake_manager()
        assert await clean_dependencies("test-app", [{"id": "", "type": "capability.mcp"}]) == []
        assert mgr.calls == []


@pytest.mark.asyncio
class TestLedgerTypeRecorded:
    async def test_install_records_canonical_type(self, fake_manager):
        """Pins the ledger ``type`` string written on install — nothing else
        asserted it, so a wrong type could be written undetected."""
        from kiro_crew.apps.dependency_ledger import get_entry

        fake_manager()
        deps = Dependencies(capabilities=CapabilityDependencies(mcp=["m"], skills=["s"]))
        await resolve_dependencies("test-app", deps)
        mcp_entry = get_entry("capability/mcp/m")
        skill_entry = get_entry("capability/skills/s")
        assert mcp_entry is not None and mcp_entry.type == "capability.mcp"
        assert skill_entry is not None and skill_entry.type == "capability.skills"
