"""Unit tests for nested artifact folders — the data layer.

Covers :class:`ArtifactFolderStore` (CRUD, cycle guard, path resolution with
mkdir -p, cascade-vs-keep delete) plus the ``ArtifactStore`` integration points:
``folder_id`` persistence, the metadata-only :meth:`ArtifactStore.set_folder`
(no version bump), and the ``list(folder=...)`` filter.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.artifacts import (
    ArtifactFolderStore,
    ArtifactNotFoundError,
    ArtifactStore,
    ArtifactValidationError,
)

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(root=tmp_path / "artifacts")


@pytest.fixture
def folders(tmp_path: Path) -> ArtifactFolderStore:
    return ArtifactFolderStore(path=tmp_path / "artifact_folders.json")


# ── folder_id on the Artifact record ─────────────────────────────────────────


class TestFolderIdField:
    def test_defaults_to_unfiled(self, store: ArtifactStore) -> None:
        art = store.create(name="A", content="x")
        assert art.folder_id == ""

    def test_create_with_folder_id(self, store: ArtifactStore) -> None:
        art = store.create(name="A", content="x", folder_id="abc123")
        assert art.folder_id == "abc123"
        # Round-trips through meta.json.
        assert store.get(art.slug).folder_id == "abc123"

    def test_legacy_meta_without_folder_id_loads_as_unfiled(
        self, store: ArtifactStore
    ) -> None:
        art = store.create(name="A", content="x")
        meta_path = store.root / art.slug / "meta.json"
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
        raw.pop("folder_id", None)  # simulate a pre-feature meta.json
        meta_path.write_text(json.dumps(raw))
        assert store.get(art.slug).folder_id == ""

    def test_null_folder_id_loads_as_unfiled(self, store: ArtifactStore) -> None:
        art = store.create(name="A", content="x")
        meta_path = store.root / art.slug / "meta.json"
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
        raw["folder_id"] = None  # explicit JSON null, not a missing key
        meta_path.write_text(json.dumps(raw))
        # Must collapse to "" (unfiled), not the string "None".
        assert store.get(art.slug).folder_id == ""


class TestSetFolder:
    def test_move_is_metadata_only_no_version_bump(self, store: ArtifactStore) -> None:
        art = store.create(name="A", content="x")
        assert art.version == 1
        moved = store.set_folder(art.slug, "fld1")
        assert moved.folder_id == "fld1"
        # Critical: moving must NOT bump the version (like retag/rename).
        assert moved.version == 1
        assert store.get(art.slug).version == 1

    def test_unfile_with_empty(self, store: ArtifactStore) -> None:
        art = store.create(name="A", content="x", folder_id="fld1")
        store.set_folder(art.slug, "")
        assert store.get(art.slug).folder_id == ""


class TestListFolderFilter:
    def test_none_returns_all(self, store: ArtifactStore) -> None:
        store.create(name="A", content="x", folder_id="f1")
        store.create(name="B", content="y")
        assert len(store.list()) == 2
        assert len(store.list(folder=None)) == 2

    def test_empty_scopes_to_unfiled(self, store: ArtifactStore) -> None:
        store.create(name="A", content="x", folder_id="f1")
        store.create(name="B", content="y")  # unfiled
        unfiled = store.list(folder="")
        assert [a.name for a in unfiled] == ["B"]

    def test_id_scopes_to_that_folder(self, store: ArtifactStore) -> None:
        store.create(name="A", content="x", folder_id="f1")
        store.create(name="B", content="y", folder_id="f2")
        assert [a.name for a in store.list(folder="f1")] == ["A"]


# ── ArtifactFolderStore CRUD ─────────────────────────────────────────────────


class TestFolderCrud:
    def test_create_root(self, folders: ArtifactFolderStore) -> None:
        f = folders.create("Reports")
        assert f["name"] == "Reports"
        assert f["parent_id"] == ""
        assert len(f["id"]) == 12
        assert [x["id"] for x in folders.list()] == [f["id"]]

    def test_create_nested(self, folders: ArtifactFolderStore) -> None:
        parent = folders.create("Reports")
        child = folders.create("Q3", parent_id=parent["id"])
        assert child["parent_id"] == parent["id"]

    def test_create_rejects_empty_name(self, folders: ArtifactFolderStore) -> None:
        with pytest.raises(ArtifactValidationError):
            folders.create("   ")

    def test_create_rejects_unknown_parent(self, folders: ArtifactFolderStore) -> None:
        with pytest.raises(ArtifactValidationError):
            folders.create("Q3", parent_id="nope")

    def test_rename(self, folders: ArtifactFolderStore) -> None:
        f = folders.create("Reports")
        renamed, _epoch = folders.rename(f["id"], "Archive")
        assert renamed["name"] == "Archive"
        assert folders.get(f["id"])["name"] == "Archive"

    def test_rename_unknown_raises(self, folders: ArtifactFolderStore) -> None:
        with pytest.raises(ArtifactNotFoundError):
            folders.rename("nope", "x")

    def test_reparent(self, folders: ArtifactFolderStore) -> None:
        a = folders.create("A")
        b = folders.create("B")
        moved = folders.reparent(b["id"], a["id"])
        assert moved["parent_id"] == a["id"]
        # Move back to root.
        assert folders.reparent(b["id"], "")["parent_id"] == ""

    def test_reparent_cycle_rejected(self, folders: ArtifactFolderStore) -> None:
        a = folders.create("A")
        b = folders.create("B", parent_id=a["id"])
        # Moving A under its own descendant B must be rejected.
        with pytest.raises(ArtifactValidationError):
            folders.reparent(a["id"], b["id"])

    def test_reparent_into_self_rejected(self, folders: ArtifactFolderStore) -> None:
        a = folders.create("A")
        with pytest.raises(ArtifactValidationError):
            folders.reparent(a["id"], a["id"])

    def test_reparent_depth_guard(self, folders: ArtifactFolderStore) -> None:
        # Merging a subtree into a deep chain must not exceed MAX_FOLDER_DEPTH —
        # the guard create()/resolve_path() enforce must also apply to reparent.
        from kiro_crew.artifacts import MAX_FOLDER_DEPTH

        chain = "/".join(f"A{i}" for i in range(MAX_FOLDER_DEPTH - 2))  # depths 0..MAX-3
        deep_leaf = folders.resolve_path(chain, create_missing=True)
        b0 = folders.create("B0")
        b1 = folders.create("B1", parent_id=b0["id"])
        folders.create("B2", parent_id=b1["id"])  # B0 subtree is 3 tall (height 2)
        with pytest.raises(ArtifactValidationError):
            folders.reparent(b0["id"], deep_leaf)
        # A shallower destination stays within the limit.
        shallower = folders.resolve_path("A0/A1", create_missing=False)
        assert folders.reparent(b0["id"], shallower)["parent_id"] == shallower

    def test_reorder(self, folders: ArtifactFolderStore) -> None:
        a = folders.create("A")
        b = folders.create("B")
        folders.reorder([{"id": a["id"], "order": 5}, {"id": b["id"], "order": 1}])
        assert folders.get(a["id"])["order"] == 5
        assert folders.get(b["id"])["order"] == 1

    def test_persists_across_reload(self, tmp_path: Path) -> None:
        path = tmp_path / "af.json"
        s1 = ArtifactFolderStore(path=path)
        f = s1.create("Reports")
        s2 = ArtifactFolderStore(path=path)
        assert [x["id"] for x in s2.list()] == [f["id"]]

    def test_orphan_parent_tolerated(self, tmp_path: Path) -> None:
        path = tmp_path / "af.json"
        path.write_text(json.dumps([{"id": "x1", "name": "Dangle", "parent_id": "gone"}]))
        s = ArtifactFolderStore(path=path)
        # A dangling parent_id must not crash the breadcrumb walk.
        assert s.breadcrumb("x1") == "Dangle"

    def test_corrupt_entries_dropped(self, tmp_path: Path) -> None:
        path = tmp_path / "af.json"
        path.write_text(json.dumps([{"name": "no-id"}, {"id": "ok", "name": "Keep"}]))
        s = ArtifactFolderStore(path=path)
        assert [x["id"] for x in s.list()] == ["ok"]


# ── Path resolution (mkdir -p semantics) ─────────────────────────────────────


class TestResolvePath:
    def test_root_refs(self, folders: ArtifactFolderStore) -> None:
        assert folders.resolve_path("") == ""
        assert folders.resolve_path("root") == ""
        assert folders.resolve_path("ROOT") == ""

    def test_id_passthrough(self, folders: ArtifactFolderStore) -> None:
        f = folders.create("Reports")
        assert folders.resolve_path(f["id"]) == f["id"]

    def test_existing_path(self, folders: ArtifactFolderStore) -> None:
        a = folders.create("A")
        b = folders.create("B", parent_id=a["id"])
        assert folders.resolve_path("A/B") == b["id"]

    def test_case_insensitive_path(self, folders: ArtifactFolderStore) -> None:
        a = folders.create("Reports")
        assert folders.resolve_path("reports") == a["id"]

    def test_missing_path_raises_without_create(self, folders: ArtifactFolderStore) -> None:
        with pytest.raises(ArtifactNotFoundError):
            folders.resolve_path("A/B/C")

    def test_mkdir_p_creates_missing(self, folders: ArtifactFolderStore) -> None:
        leaf = folders.resolve_path("A/B/C", create_missing=True)
        assert folders.breadcrumb(leaf) == "A/B/C"
        # Idempotent: resolving again returns the same leaf, no dupes.
        assert folders.resolve_path("A/B/C", create_missing=True) == leaf
        assert len(folders.list()) == 3

    def test_mkdir_p_idempotent_for_long_segments(self, folders: ArtifactFolderStore) -> None:
        # A >100-char segment is truncated by _clean_name on create; the lookup
        # must normalize through the same truncation, or every repeat resolve of
        # the same long path would mint a duplicate folder.
        long_seg = "x" * 150
        first = folders.resolve_path(long_seg, create_missing=True)
        assert folders.resolve_path(long_seg, create_missing=True) == first
        # The truncated stored name also resolves to the same folder.
        assert folders.resolve_path(long_seg[:100], create_missing=True) == first
        assert len(folders.list()) == 1

    def test_breadcrumb(self, folders: ArtifactFolderStore) -> None:
        leaf = folders.resolve_path("X/Y", create_missing=True)
        assert folders.breadcrumb(leaf) == "X/Y"
        assert folders.breadcrumb("") == ""

    def test_mkdir_p_rolls_back_on_depth_failure(self, folders: ArtifactFolderStore) -> None:
        # A path deeper than MAX_FOLDER_DEPTH must fail all-or-nothing: no
        # partial folders left behind (in-memory state stays consistent with disk).
        from kiro_crew.artifacts import MAX_FOLDER_DEPTH

        deep = "/".join(f"L{i}" for i in range(MAX_FOLDER_DEPTH + 2))
        with pytest.raises(ArtifactValidationError):
            folders.resolve_path(deep, create_missing=True)
        assert folders.list() == []  # every appended segment rolled back
        # A reload confirms nothing was persisted either.
        assert ArtifactFolderStore(path=folders._path).list() == []


# ── Delete: cascade vs keep ──────────────────────────────────────────────────


class TestFolderDelete:
    def test_keep_reparents_children_and_artifacts(
        self, store: ArtifactStore, folders: ArtifactFolderStore
    ) -> None:
        root = folders.create("Root")
        mid = folders.create("Mid", parent_id=root["id"])
        leaf = folders.create("Leaf", parent_id=mid["id"])
        art = store.create(name="doc", content="x", folder_id=mid["id"])

        summary = folders.delete(mid["id"], delete_contents=False, artifact_store=store)

        # Only Mid is removed; Root + Leaf survive.
        ids = {f["id"] for f in folders.list()}
        assert root["id"] in ids and leaf["id"] in ids and mid["id"] not in ids
        # Leaf re-parents up to Root (Mid's parent).
        assert folders.get(leaf["id"])["parent_id"] == root["id"]
        # The artifact re-parents to Root and is NOT deleted.
        assert store.get(art.slug).folder_id == root["id"]
        assert summary["reparented_to"] == root["id"]
        assert art.slug in summary["reparented_artifact_slugs"]

    def test_keep_at_root_reparents_to_unfiled(
        self, store: ArtifactStore, folders: ArtifactFolderStore
    ) -> None:
        top = folders.create("Top")
        art = store.create(name="doc", content="x", folder_id=top["id"])
        folders.delete(top["id"], delete_contents=False, artifact_store=store)
        assert store.get(art.slug).folder_id == ""  # unfiled (root)

    def test_cascade_deletes_subtree_and_artifacts(
        self, store: ArtifactStore, folders: ArtifactFolderStore
    ) -> None:
        root = folders.create("Root")
        mid = folders.create("Mid", parent_id=root["id"])
        a1 = store.create(name="a1", content="x", folder_id=root["id"])
        a2 = store.create(name="a2", content="y", folder_id=mid["id"])
        outside = store.create(name="out", content="z")  # unfiled, must survive

        summary = folders.delete(root["id"], delete_contents=True, artifact_store=store)

        assert folders.list() == []  # whole subtree gone
        with pytest.raises(ArtifactNotFoundError):
            store.get(a1.slug)
        with pytest.raises(ArtifactNotFoundError):
            store.get(a2.slug)
        # Unrelated unfiled artifact is untouched.
        assert store.get(outside.slug).folder_id == ""
        assert set(summary["deleted_artifact_slugs"]) == {a1.slug, a2.slug}

    def test_delete_unknown_raises(
        self, store: ArtifactStore, folders: ArtifactFolderStore
    ) -> None:
        with pytest.raises(ArtifactNotFoundError):
            folders.delete("nope", delete_contents=False, artifact_store=store)


# ── Counts ───────────────────────────────────────────────────────────────────


class TestItemCounts:
    def test_direct_counts(self, store: ArtifactStore, folders: ArtifactFolderStore) -> None:
        f = folders.create("Reports")
        store.create(name="a", content="x", folder_id=f["id"])
        store.create(name="b", content="y", folder_id=f["id"])
        store.create(name="c", content="z")  # unfiled
        counts = folders.item_counts(store)
        assert counts == {f["id"]: 2}
        enriched = {x["id"]: x["item_count"] for x in folders.list_with_counts(store)}
        assert enriched[f["id"]] == 2


class TestFolderColor:
    def test_set_and_clear_color(self, folders: ArtifactFolderStore) -> None:
        f = folders.create("Reports")
        updated = folders.set_color(f["id"], "#FF8800")
        assert updated["color"] == "#ff8800"  # normalized lowercase
        cleared = folders.set_color(f["id"], "")
        assert "color" not in cleared

    def test_invalid_color_rejected(self, folders: ArtifactFolderStore) -> None:
        f = folders.create("Reports")
        for bad in ("red", "#fff", "#12345g", "javascript:alert(1)"):
            with pytest.raises(ArtifactValidationError):
                folders.set_color(f["id"], bad)

    def test_create_with_color(self, folders: ArtifactFolderStore) -> None:
        f = folders.create("Reports", color="#3b82f6")
        assert f["color"] == "#3b82f6"

    def test_create_with_invalid_color_rejected(self, folders: ArtifactFolderStore) -> None:
        with pytest.raises(ArtifactValidationError):
            folders.create("Reports", color="blue")

    def test_color_persists_across_reload(self, folders: ArtifactFolderStore, tmp_path) -> None:
        f = folders.create("Reports", color="#22c55e")
        reloaded = ArtifactFolderStore(path=folders._path)
        assert reloaded.get(f["id"])["color"] == "#22c55e"
