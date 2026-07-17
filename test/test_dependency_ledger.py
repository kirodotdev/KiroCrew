"""Tests for kiro_crew.apps.dependency_ledger — reference-counted dependency tracking."""
from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from kiro_crew.apps.dependency_ledger import (
    LedgerEntry,
    classify_for_uninstall,
    get_entry,
    list_by_app,
    record_install,
    record_uninstall,
)


@pytest.fixture(autouse=True)
def _ledger_home(tmp_path, monkeypatch):
    """Isolate ledger to temp directory."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "kirocrew-home"))


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

class TestLedgerCRUD:
    def test_record_and_get(self):
        record_install("aim/mcp/aws-docs", "app-a", "aim.mcp")
        entry = get_entry("aim/mcp/aws-docs")
        assert entry is not None
        assert entry.installedBy == ["app-a"]
        assert entry.type == "aim.mcp"
        assert entry.installedAt != ""

    def test_record_multiple_apps(self):
        record_install("aim/mcp/shared", "app-a", "aim.mcp")
        record_install("aim/mcp/shared", "app-b", "aim.mcp")
        entry = get_entry("aim/mcp/shared")
        assert entry is not None
        assert sorted(entry.installedBy) == ["app-a", "app-b"]

    def test_record_idempotent(self):
        record_install("aim/mcp/x", "app-a", "aim.mcp")
        record_install("aim/mcp/x", "app-a", "aim.mcp")
        entry = get_entry("aim/mcp/x")
        assert entry is not None
        assert entry.installedBy == ["app-a"]  # no duplicate

    def test_uninstall_removes_reference(self):
        record_install("aim/mcp/x", "app-a", "aim.mcp")
        record_install("aim/mcp/x", "app-b", "aim.mcp")
        record_uninstall("aim/mcp/x", "app-a")
        entry = get_entry("aim/mcp/x")
        assert entry is not None
        assert entry.installedBy == ["app-b"]

    def test_uninstall_last_reference_deletes_entry(self):
        record_install("aim/mcp/x", "app-a", "aim.mcp")
        record_uninstall("aim/mcp/x", "app-a")
        assert get_entry("aim/mcp/x") is None

    def test_uninstall_nonexistent_noop(self):
        record_uninstall("aim/mcp/nonexistent", "app-a")  # should not raise

    def test_get_nonexistent(self):
        assert get_entry("aim/mcp/nonexistent") is None

    def test_list_by_app(self):
        record_install("aim/mcp/a", "app-x", "aim.mcp")
        record_install("aim/skills/b", "app-x", "aim.skills")
        record_install("aim/mcp/c", "app-y", "aim.mcp")
        entries = list_by_app("app-x")
        keys = {k for k, _ in entries}
        assert keys == {"aim/mcp/a", "aim/skills/b"}

    def test_list_by_app_empty(self):
        assert list_by_app("nonexistent") == []


class TestClassifyForUninstall:
    def test_removable(self):
        record_install("aim/mcp/only-mine", "app-a", "aim.mcp")
        result = classify_for_uninstall("app-a", ["aim/mcp/only-mine"])
        assert len(result["removable"]) == 1
        assert result["removable"][0]["id"] == "aim/mcp/only-mine"
        assert result["shared"] == []
        assert result["userInstalled"] == []

    def test_shared(self):
        record_install("aim/mcp/shared", "app-a", "aim.mcp")
        record_install("aim/mcp/shared", "app-b", "aim.mcp")
        result = classify_for_uninstall("app-a", ["aim/mcp/shared"])
        assert len(result["shared"]) == 1
        assert result["shared"][0]["usedBy"] == ["app-b"]
        assert result["removable"] == []

    def test_user_installed(self):
        result = classify_for_uninstall("app-a", ["aim/mcp/user-dep"])
        assert len(result["userInstalled"]) == 1
        assert result["removable"] == []
        assert result["shared"] == []

    def test_mixed(self):
        record_install("aim/mcp/mine", "app-a", "aim.mcp")
        record_install("aim/mcp/shared", "app-a", "aim.mcp")
        record_install("aim/mcp/shared", "app-b", "aim.mcp")
        result = classify_for_uninstall(
            "app-a",
            ["aim/mcp/mine", "aim/mcp/shared", "aim/mcp/user-dep"],
        )
        assert len(result["removable"]) == 1
        assert len(result["shared"]) == 1
        assert len(result["userInstalled"]) == 1


class TestLedgerEntry:
    def test_round_trip(self):
        entry = LedgerEntry(installedBy=["a", "b"], installedAt="2026-01-01", type="aim.mcp")
        d = entry.to_dict()
        restored = LedgerEntry.from_dict(d)
        assert restored.installedBy == entry.installedBy
        assert restored.type == entry.type


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------

_app_name = st.from_regex(r"[a-z][a-z0-9\-]{0,15}", fullmatch=True)
_dep_key = st.from_regex(r"aim/(mcp|skills|agents)/[a-z][a-z0-9\-]{0,20}", fullmatch=True)


def _clear_ledger() -> None:
    """Remove the ledger file to reset state between hypothesis examples."""
    from kiro_crew.apps.dependency_ledger import _ledger_path
    path = _ledger_path()
    if path.is_file():
        path.unlink()


class TestLedgerProperties:
    # Feature: app-classification-redesign, Property 6: 账本 record_install 正确性
    @given(
        dep=_dep_key,
        apps=st.lists(_app_name, min_size=1, max_size=10, unique=True),
    )
    @settings(max_examples=200)
    def test_record_install_correctness(self, dep, apps):
        """**Validates: Requirements 6.2, 6.3**"""
        _clear_ledger()
        for app in apps:
            record_install(dep, app, "aim.mcp")
        entry = get_entry(dep)
        assert entry is not None
        assert sorted(entry.installedBy) == sorted(apps)
        # No duplicates
        assert len(entry.installedBy) == len(set(entry.installedBy))

    # Feature: app-classification-redesign, Property 7: classify_for_uninstall 分类正确性
    @given(
        target_app=_app_name,
        other_apps=st.lists(_app_name, min_size=0, max_size=5, unique=True),
        removable_deps=st.lists(_dep_key, min_size=0, max_size=3, unique=True),
        shared_deps=st.lists(_dep_key, min_size=0, max_size=3, unique=True),
    )
    @settings(max_examples=200)
    def test_classify_correctness(self, target_app, other_apps, removable_deps, shared_deps):
        """**Validates: Requirements 6.6, 6.7, 6.8**"""
        _clear_ledger()
        # Ensure target_app not in other_apps
        other_apps = [a for a in other_apps if a != target_app]
        # Ensure no overlap between dep lists
        shared_deps = [d for d in shared_deps if d not in removable_deps]

        # Setup: removable deps only have target_app
        for dep in removable_deps:
            record_install(dep, target_app, "aim.mcp")
        # Setup: shared deps have target_app + at least one other
        for dep in shared_deps:
            record_install(dep, target_app, "aim.mcp")
            for other in other_apps[:1] or ["other-app"]:
                record_install(dep, other, "aim.mcp")

        user_deps = ["aim/mcp/USER-only-dep"]
        all_deps = removable_deps + shared_deps + user_deps

        result = classify_for_uninstall(target_app, all_deps)

        removable_ids = {d["id"] for d in result["removable"]}
        shared_ids = {d["id"] for d in result["shared"]}
        user_ids = {d["id"] for d in result["userInstalled"]}

        for dep in removable_deps:
            assert dep in removable_ids
        for dep in shared_deps:
            assert dep in shared_ids
        for dep in user_deps:
            assert dep in user_ids
