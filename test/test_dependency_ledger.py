"""Tests for kiro_crew.apps.dependency_ledger — reference-counted dependency tracking."""
from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from kiro_crew.apps.dependency_ledger import (
    LedgerEntry,
    classify_and_clean_for_uninstall,
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
        record_install("capability/mcp/aws-docs", "app-a", "capability.mcp")
        entry = get_entry("capability/mcp/aws-docs")
        assert entry is not None
        assert entry.installedBy == ["app-a"]
        assert entry.type == "capability.mcp"
        assert entry.installedAt != ""

    def test_record_multiple_apps(self):
        record_install("capability/mcp/shared", "app-a", "capability.mcp")
        record_install("capability/mcp/shared", "app-b", "capability.mcp")
        entry = get_entry("capability/mcp/shared")
        assert entry is not None
        assert sorted(entry.installedBy) == ["app-a", "app-b"]

    def test_record_idempotent(self):
        record_install("capability/mcp/x", "app-a", "capability.mcp")
        record_install("capability/mcp/x", "app-a", "capability.mcp")
        entry = get_entry("capability/mcp/x")
        assert entry is not None
        assert entry.installedBy == ["app-a"]  # no duplicate

    def test_uninstall_removes_reference(self):
        record_install("capability/mcp/x", "app-a", "capability.mcp")
        record_install("capability/mcp/x", "app-b", "capability.mcp")
        record_uninstall("capability/mcp/x", "app-a")
        entry = get_entry("capability/mcp/x")
        assert entry is not None
        assert entry.installedBy == ["app-b"]

    def test_uninstall_last_reference_deletes_entry(self):
        record_install("capability/mcp/x", "app-a", "capability.mcp")
        record_uninstall("capability/mcp/x", "app-a")
        assert get_entry("capability/mcp/x") is None

    def test_uninstall_nonexistent_noop(self):
        record_uninstall("capability/mcp/nonexistent", "app-a")  # should not raise

    def test_get_nonexistent(self):
        assert get_entry("capability/mcp/nonexistent") is None

    def test_list_by_app(self):
        record_install("capability/mcp/a", "app-x", "capability.mcp")
        record_install("capability/skills/b", "app-x", "capability.skills")
        record_install("capability/mcp/c", "app-y", "capability.mcp")
        entries = list_by_app("app-x")
        keys = {k for k, _ in entries}
        assert keys == {"capability/mcp/a", "capability/skills/b"}

    def test_list_by_app_empty(self):
        assert list_by_app("nonexistent") == []


class TestClassifyForUninstall:
    def test_removable(self):
        record_install("capability/mcp/only-mine", "app-a", "capability.mcp")
        result = classify_for_uninstall("app-a", ["capability/mcp/only-mine"])
        assert len(result["removable"]) == 1
        assert result["removable"][0]["id"] == "capability/mcp/only-mine"
        assert result["shared"] == []
        assert result["userInstalled"] == []

    def test_shared(self):
        record_install("capability/mcp/shared", "app-a", "capability.mcp")
        record_install("capability/mcp/shared", "app-b", "capability.mcp")
        result = classify_for_uninstall("app-a", ["capability/mcp/shared"])
        assert len(result["shared"]) == 1
        assert result["shared"][0]["usedBy"] == ["app-b"]
        assert result["removable"] == []

    def test_user_installed(self):
        result = classify_for_uninstall("app-a", ["capability/mcp/user-dep"])
        assert len(result["userInstalled"]) == 1
        assert result["removable"] == []
        assert result["shared"] == []

    def test_mixed(self):
        record_install("capability/mcp/mine", "app-a", "capability.mcp")
        record_install("capability/mcp/shared", "app-a", "capability.mcp")
        record_install("capability/mcp/shared", "app-b", "capability.mcp")
        result = classify_for_uninstall(
            "app-a",
            ["capability/mcp/mine", "capability/mcp/shared", "capability/mcp/user-dep"],
        )
        assert len(result["removable"]) == 1
        assert len(result["shared"]) == 1
        assert len(result["userInstalled"]) == 1


class TestLedgerEntry:
    def test_round_trip(self):
        entry = LedgerEntry(installedBy=["a", "b"], installedAt="2026-01-01", type="capability.mcp")
        d = entry.to_dict()
        restored = LedgerEntry.from_dict(d)
        assert restored.installedBy == entry.installedBy
        assert restored.type == entry.type


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------

_app_name = st.from_regex(r"[a-z][a-z0-9\-]{0,15}", fullmatch=True)
_dep_key = st.from_regex(r"capability/(mcp|skills|agents)/[a-z][a-z0-9\-]{0,20}", fullmatch=True)


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
            record_install(dep, app, "capability.mcp")
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
            record_install(dep, target_app, "capability.mcp")
        # Setup: shared deps have target_app + at least one other
        for dep in shared_deps:
            record_install(dep, target_app, "capability.mcp")
            for other in other_apps[:1] or ["other-app"]:
                record_install(dep, other, "capability.mcp")

        user_deps = ["capability/mcp/USER-only-dep"]
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


# ---------------------------------------------------------------------------
# Legacy key compatibility (pre-rename ledgers)
# ---------------------------------------------------------------------------


def _write_raw_ledger(entries: dict) -> None:
    """Seed the ledger file directly, bypassing the record_* helpers."""
    import json

    from kiro_crew.apps.dependency_ledger import _ledger_path

    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries), encoding="utf-8")


def _read_raw_ledger() -> dict:
    """Read the ledger file directly, bypassing key-resolution helpers."""
    import json

    from kiro_crew.apps.dependency_ledger import _ledger_path

    path = _ledger_path()
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


class TestLegacyKeyCompat:
    """A ledger written before the ``aim/*`` → ``capability/*`` rename must keep
    resolving — otherwise an upgrade orphans every tracked dependency, and the
    refcount that guards shared deps silently resets to zero."""

    def test_get_entry_reads_legacy_key(self):
        _write_raw_ledger({
            "aim/mcp/legacy-dep": {
                "installedBy": ["app-a"], "installedAt": "2026-01-01", "type": "aim.mcp",
            }
        })
        entry = get_entry("capability/mcp/legacy-dep")
        assert entry is not None
        assert entry.installedBy == ["app-a"]

    def test_record_install_accrues_onto_legacy_key(self):
        """A second app must join the EXISTING legacy entry, not fork a new
        canonical one — two keys for one dep would let uninstall remove a dep
        another app still uses."""
        _write_raw_ledger({
            "aim/mcp/shared-dep": {
                "installedBy": ["app-a"], "installedAt": "2026-01-01", "type": "aim.mcp",
            }
        })
        record_install("capability/mcp/shared-dep", "app-b", "capability.mcp")
        entry = get_entry("capability/mcp/shared-dep")
        assert entry is not None
        assert entry.installedBy == ["app-a", "app-b"]

    def test_record_uninstall_prunes_legacy_key(self):
        """Assert on the RAW ledger, not via get_entry: get_entry shares the same
        legacy-key fallback, so a broken fallback would make it return None for the
        wrong reason and hide a leaked entry."""
        _write_raw_ledger({
            "aim/mcp/gone": {
                "installedBy": ["app-a"], "installedAt": "2026-01-01", "type": "aim.mcp",
            }
        })
        record_uninstall("capability/mcp/gone", "app-a")
        assert _read_raw_ledger() == {}

    def test_classify_sees_legacy_entry_as_removable(self):
        """Without legacy resolution this would classify as 'user installed' and
        the dep would never be cleaned up."""
        _write_raw_ledger({
            "aim/mcp/mine": {
                "installedBy": ["app-a"], "installedAt": "2026-01-01", "type": "aim.mcp",
            }
        })
        result = classify_for_uninstall("app-a", ["capability/mcp/mine"])
        assert len(result["removable"]) == 1
        assert result["userInstalled"] == []

    def test_canonical_key_wins_when_both_present(self):
        _write_raw_ledger({
            "aim/mcp/dup": {
                "installedBy": ["old-app"], "installedAt": "2026-01-01", "type": "aim.mcp",
            },
            "capability/mcp/dup": {
                "installedBy": ["new-app"], "installedAt": "2026-02-02", "type": "capability.mcp",
            },
        })
        entry = get_entry("capability/mcp/dup")
        assert entry is not None
        assert entry.installedBy == ["new-app"]
        # A second app must accrue onto the CANONICAL row, leaving the legacy
        # row untouched — otherwise the two spellings drift apart.
        record_install("capability/mcp/dup", "app-c", "capability.mcp")
        raw = _read_raw_ledger()
        assert raw["capability/mcp/dup"]["installedBy"] == ["new-app", "app-c"]
        assert raw["aim/mcp/dup"]["installedBy"] == ["old-app"]


class TestDeclaredCapabilityKeys:
    def test_reads_capabilities_key(self):
        from kiro_crew.apps.dependency_ledger import declared_capability_keys

        keys = declared_capability_keys({"capabilities": {"mcp": ["a"], "skills": ["b"]}})
        assert keys == ["capability/mcp/a", "capability/skills/b"]

    def test_reads_deprecated_alias(self):
        from kiro_crew.apps.dependency_ledger import declared_capability_keys

        assert declared_capability_keys({"aim": {"mcp": ["a"]}}) == ["capability/mcp/a"]

    def test_canonical_key_takes_precedence_over_alias(self):
        from kiro_crew.apps.dependency_ledger import declared_capability_keys

        keys = declared_capability_keys({"capabilities": {"mcp": ["new"]}, "aim": {"mcp": ["old"]}})
        assert keys == ["capability/mcp/new"]

    def test_object_entries_and_blanks(self):
        from kiro_crew.apps.dependency_ledger import declared_capability_keys

        keys = declared_capability_keys({
            "capabilities": {"mcp": [{"id": "obj"}, "", {"id": ""}, "plain"]}
        })
        assert keys == ["capability/mcp/obj", "capability/mcp/plain"]

    def test_malformed_shapes_do_not_raise(self):
        from kiro_crew.apps.dependency_ledger import declared_capability_keys

        assert declared_capability_keys({}) == []
        assert declared_capability_keys({"capabilities": "nope"}) == []
        assert declared_capability_keys({"capabilities": {"mcp": "nope"}}) == []


class TestClassifyAndCleanForUninstall:
    """The only ledger WRITE path taken on uninstall (``routes.py``), and the one
    this change edited to add legacy-key resolution — previously untested."""

    def test_sole_owner_dep_is_pruned(self):
        _write_raw_ledger({
            "capability/mcp/mine": {
                "installedBy": ["app-a"], "installedAt": "2026-01-01", "type": "capability.mcp",
            }
        })
        result = classify_and_clean_for_uninstall("app-a", ["capability/mcp/mine"])
        assert [d["id"] for d in result["removable"]] == ["capability/mcp/mine"]
        assert _read_raw_ledger() == {}

    def test_shared_dep_keeps_other_owner(self):
        """The refcount that stops one app's uninstall from deleting a dep
        another app still uses."""
        _write_raw_ledger({
            "capability/mcp/shared": {
                "installedBy": ["app-a", "app-b"],
                "installedAt": "2026-01-01",
                "type": "capability.mcp",
            }
        })
        result = classify_and_clean_for_uninstall("app-a", ["capability/mcp/shared"])
        assert [d["id"] for d in result["shared"]] == ["capability/mcp/shared"]
        assert _read_raw_ledger()["capability/mcp/shared"]["installedBy"] == ["app-b"]

    def test_legacy_shared_dep_keeps_other_owner(self):
        """Same guarantee across the rename: without legacy resolution this
        would classify as untracked and app-b's dependency would be dropped."""
        _write_raw_ledger({
            "aim/mcp/shared": {
                "installedBy": ["app-a", "app-b"],
                "installedAt": "2026-01-01",
                "type": "aim.mcp",
            }
        })
        result = classify_and_clean_for_uninstall("app-a", ["capability/mcp/shared"])
        assert result["userInstalled"] == []
        assert [d["id"] for d in result["shared"]] == ["capability/mcp/shared"]
        assert _read_raw_ledger()["aim/mcp/shared"]["installedBy"] == ["app-b"]

    def test_legacy_sole_owner_dep_is_pruned(self):
        _write_raw_ledger({
            "aim/mcp/mine": {
                "installedBy": ["app-a"], "installedAt": "2026-01-01", "type": "aim.mcp",
            }
        })
        classify_and_clean_for_uninstall("app-a", ["capability/mcp/mine"])
        assert _read_raw_ledger() == {}

    def test_keep_specific_drops_ownership_on_legacy_key(self):
        """``keep`` is matched on the CANONICAL declared id while the ledger row
        is legacy-spelled; the ownership drop must still land."""
        _write_raw_ledger({
            "aim/mcp/keepme": {
                "installedBy": ["app-a", "app-b"],
                "installedAt": "2026-01-01",
                "type": "aim.mcp",
            }
        })
        classify_and_clean_for_uninstall(
            "app-a", ["capability/mcp/keepme"], keep_specific=["capability/mcp/keepme"],
        )
        # Shared row: app-a's reference removed, app-b retained.
        assert _read_raw_ledger()["aim/mcp/keepme"]["installedBy"] == ["app-b"]

    def test_untracked_dep_is_left_alone(self):
        _write_raw_ledger({
            "capability/mcp/theirs": {
                "installedBy": ["other-app"], "installedAt": "2026-01-01",
                "type": "capability.mcp",
            }
        })
        result = classify_and_clean_for_uninstall("app-a", ["capability/mcp/theirs"])
        assert [d["id"] for d in result["userInstalled"]] == ["capability/mcp/theirs"]
        assert _read_raw_ledger()["capability/mcp/theirs"]["installedBy"] == ["other-app"]


class TestCanonicalDepKey:
    """``keep_specific`` arrives from the CLIENT (an uninstall request body). A
    dashboard whose uninstall preview was fetched from a pre-rename build echoes
    legacy ids back, so the uninstall path must normalize them before comparing
    against classification output — otherwise an explicit "keep this dependency"
    is silently ignored and the dep is deleted."""

    def test_normalizes_legacy_prefix(self):
        from kiro_crew.apps.dependency_ledger import canonical_dep_key

        assert canonical_dep_key("aim/mcp/x") == "capability/mcp/x"
        assert canonical_dep_key("aim/skills/Pkg") == "capability/skills/Pkg"

    def test_is_idempotent_for_canonical_keys(self):
        from kiro_crew.apps.dependency_ledger import canonical_dep_key

        assert canonical_dep_key("capability/mcp/x") == "capability/mcp/x"

    def test_leaves_unrelated_strings_untouched(self):
        from kiro_crew.apps.dependency_ledger import canonical_dep_key

        for s in ("command:node", "", "aimless/mcp/x", "other/mcp/x"):
            assert canonical_dep_key(s) == s

    def test_legacy_keep_id_preserves_the_dependency(self):
        """The regression itself: a legacy keep id must still spare the dep."""
        from kiro_crew.apps.dependency_ledger import canonical_dep_key

        _write_raw_ledger({
            "capability/mcp/keepme": {
                "installedBy": ["app-a"], "installedAt": "2026-01-01",
                "type": "capability.mcp",
            }
        })
        # Client sends the PRE-RENAME id; declared deps are canonical.
        legacy_keep = ["aim/mcp/keepme"]
        keep_canonical = [canonical_dep_key(k) for k in legacy_keep]
        result = classify_and_clean_for_uninstall(
            "app-a", ["capability/mcp/keepme"], keep_specific=keep_canonical,
        )
        removable = [
            d for d in result["removable"] if d.get("id") not in keep_canonical
        ]
        # Nothing is handed to the uninstaller — the user's keep is honored.
        assert removable == []


class TestUncleanableTypesArePreserved:
    """A dependency type with no cleanup operation must keep its ledger row.

    Deleting the row for something KiroCrew cannot actually uninstall leaves the
    installed package on disk with nothing recording that it was ever referenced —
    an untraceable orphan. Ownership is dropped instead, so it reads as
    user-installed and stays reclaimable."""

    def test_sole_owner_agents_row_is_retained_ownerless(self):
        """The orphan case: sole owner -> classified REMOVABLE -> the row would be
        popped. It must survive ownerless, because nothing can uninstall it."""
        _write_raw_ledger({
            "capability/agents/Pkg": {
                "installedBy": ["app-a"], "installedAt": "2026-01-01",
                "type": "capability.agents",
            }
        })
        result = classify_and_clean_for_uninstall("app-a", ["capability/agents/Pkg"])
        assert [d["id"] for d in result["removable"]] == ["capability/agents/Pkg"]
        raw = _read_raw_ledger()
        assert "capability/agents/Pkg" in raw, "agents row was orphaned"
        assert raw["capability/agents/Pkg"]["installedBy"] == []

    def test_retained_row_reclassifies_as_user_installed(self):
        """The ownerless row must not re-appear as removable for the same app."""
        _write_raw_ledger({
            "capability/agents/Pkg": {
                "installedBy": ["app-a"], "installedAt": "2026-01-01",
                "type": "capability.agents",
            }
        })
        classify_and_clean_for_uninstall("app-a", ["capability/agents/Pkg"])
        again = classify_and_clean_for_uninstall("app-a", ["capability/agents/Pkg"])
        assert again["removable"] == []
        assert [d["id"] for d in again["userInstalled"]] == ["capability/agents/Pkg"]

    def test_agents_row_with_other_owner_is_not_deleted(self):
        _write_raw_ledger({
            "capability/agents/Pkg": {
                "installedBy": ["app-a", "app-b"], "installedAt": "2026-01-01",
                "type": "capability.agents",
            }
        })
        classify_and_clean_for_uninstall("app-a", ["capability/agents/Pkg"])
        assert _read_raw_ledger()["capability/agents/Pkg"]["installedBy"] == ["app-b"]

    def test_legacy_agents_row_is_not_orphaned(self):
        """The reported case: a pre-rename ``aim/agents/X`` row must not be
        dropped, since cleanup skips ``agents`` and cannot remove the package."""
        _write_raw_ledger({
            "aim/agents/Pkg": {
                "installedBy": ["app-a"], "installedAt": "2026-01-01",
                "type": "aim.agents",
            }
        })
        classify_and_clean_for_uninstall("app-a", ["capability/agents/Pkg"])
        raw = _read_raw_ledger()
        assert "aim/agents/Pkg" in raw, "legacy agents row was orphaned"
        assert raw["aim/agents/Pkg"]["installedBy"] == []

    def test_mcp_rows_are_still_deleted(self):
        """Cleanable types keep the original delete behavior."""
        _write_raw_ledger({
            "capability/mcp/Dep": {
                "installedBy": ["app-a"], "installedAt": "2026-01-01",
                "type": "capability.mcp",
            }
        })
        classify_and_clean_for_uninstall("app-a", ["capability/mcp/Dep"])
        assert _read_raw_ledger() == {}

    def test_is_uncleanable_accepts_every_spelling(self):
        from kiro_crew.apps.dependency_ledger import _is_uncleanable

        for spelling in ("agents", "capability.agents", "aim.agents"):
            assert _is_uncleanable(spelling) is True
        for spelling in ("mcp", "capability.mcp", "aim.skills", "", "unknown"):
            assert _is_uncleanable(spelling) is False
