"""Byte-level contract of the artifact store's persisted files and errors.

The store is split across :mod:`kiro_crew.artifacts` (the facade that owns the
lock, the fenced file IO and the singletons) and the ``kiro_crew.artifact_store``
package (models, field rules, wire codec, comment threads, folders). Nothing a
caller can observe may depend on where a rule lives, so this file pins the
observable surface directly: the exact bytes of ``meta.json``,
``comments.json`` and ``artifact_folders.json``, the dataclass field order that
fixes the key order of every HTTP response, the directory layout, the exact
validation and generation-token error messages, the change-listener and
metrics ordering, and the module-attribute seams tests and callers patch, on the
facade and on the owner modules.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import importlib.util
import inspect
import json
import re
import threading
import time
import uuid
from pathlib import Path

import pytest
from source_corpus import repo_files_named, repo_root

from kiro_crew import artifacts as art_mod
from kiro_crew.artifacts import (
    EXPECT_ABSENT,
    Artifact,
    ArtifactAlreadyExistsError,
    ArtifactComment,
    ArtifactError,
    ArtifactFolderStore,
    ArtifactNotFoundError,
    ArtifactPublication,
    ArtifactReplacedError,
    ArtifactStillPublishedError,
    ArtifactStore,
    ArtifactValidationError,
    ForkMetadata,
    ImageMetadata,
)

NOW = "2026-01-02T03:04:05.000006+00:00"
TOKEN = "0123456789abcdef" * 4


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> ArtifactStore:
    monkeypatch.setattr(art_mod, "_now_iso", lambda: NOW)
    monkeypatch.setattr(art_mod, "_new_content_token", lambda: TOKEN)
    return ArtifactStore(root=tmp_path / "artifacts")


def _png() -> bytes:
    # 8-byte signature + IHDR declaring a 3x2 image; nothing past the header is read.
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + (3).to_bytes(4, "big")
        + (2).to_bytes(4, "big")
    )


class TestPersistedBytes:
    def test_meta_json_bytes(self, store: ArtifactStore) -> None:
        art = store.create(
            name="Golden Doc",
            content="<div>x</div>",
            tags=["ops", "b"],
            description="d",
            source="manual",
            session_key="chat-1-2",
        )
        raw = (store.root / art.slug / "meta.json").read_text(encoding="utf-8")
        assert raw == (
            "{\n"
            '  "auto_registered": false,\n'
            f'  "content_token": "{TOKEN}",\n'
            f'  "created_at": "{NOW}",\n'
            '  "description": "d",\n'
            '  "events": [\n'
            "    {\n"
            '      "by": "manual",\n'
            f'      "ts": "{NOW}",\n'
            '      "type": "created",\n'
            '      "version": 1\n'
            "    }\n"
            "  ],\n"
            '  "events_backfilled": true,\n'
            '  "folder_id": "",\n'
            '  "fork_metadata": null,\n'
            '  "image": null,\n'
            '  "kind": "widget",\n'
            '  "kind_auto": false,\n'
            '  "name": "Golden Doc",\n'
            '  "pinned": false,\n'
            '  "publication": null,\n'
            '  "session_key": "chat-1-2",\n'
            '  "slug": "golden-doc",\n'
            '  "source": "manual",\n'
            '  "source_copy_only": false,\n'
            '  "source_path": "",\n'
            '  "source_root": "",\n'
            '  "tags": [\n'
            '    "ops",\n'
            '    "b"\n'
            "  ],\n"
            f'  "updated_at": "{NOW}",\n'
            '  "version": 1,\n'
            '  "version_kinds": {\n'
            '    "1": "widget"\n'
            "  },\n"
            '  "webapp_metadata": null\n'
            "}"
        )

    def test_full_comment_bytes(self, store: ArtifactStore) -> None:
        art = store.create(name="c", content="hello")
        store.add_comment(
            art.slug,
            ArtifactComment(
                id="c1",
                origin="p:r1",
                provider="p",
                scope="shared",
                author="me",
                is_agent=True,
                body="hi",
                anchor_quote="x",
                anchor_prefix="a",
                anchor_suffix="b",
                anchor_start_offset=1,
                anchor_end_offset=2,
                anchor_version=1,
                thread_id="root-0",
                parent_id="root-0",
                status="review",
                target_provider="p",
                anchor_orphaned=True,
                target_external_id="e1",
                sync_state="synced",
                created_at="t1",
                updated_at="t2",
            ),
        )
        raw = (store.root / art.slug / "comments.json").read_text(encoding="utf-8")
        assert json.loads(raw) == [
            {
                "id": "c1",
                "origin": "p:r1",
                "scope": "shared",
                "author": "me",
                "is_agent": True,
                "body": "hi",
                "thread_id": "root-0",
                "status": "review",
                "sync_state": "synced",
                "created_at": "t1",
                "updated_at": "t2",
                "provider": "p",
                "parent_id": "root-0",
                "target_provider": "p",
                "target_external_id": "e1",
                "anchor_orphaned": True,
                "anchor_quote": "x",
                "anchor_prefix": "a",
                "anchor_suffix": "b",
                "anchor_start_offset": 1,
                "anchor_end_offset": 2,
                "anchor_version": 1,
            }
        ]
        # Key ORDER is part of the file: fixed keys first, optional keys after.
        assert list(json.loads(raw)[0]) == [
            "id",
            "origin",
            "scope",
            "author",
            "is_agent",
            "body",
            "thread_id",
            "status",
            "sync_state",
            "created_at",
            "updated_at",
            "provider",
            "parent_id",
            "target_provider",
            "target_external_id",
            "anchor_orphaned",
            "anchor_quote",
            "anchor_prefix",
            "anchor_suffix",
            "anchor_start_offset",
            "anchor_end_offset",
            "anchor_version",
        ]
        assert raw.startswith('[\n  {\n    "id": "c1",\n')

    def test_minimal_comment_bytes_and_tolerant_reload(self, store: ArtifactStore) -> None:
        art = store.create(name="c", content="hello")
        store.add_comment(art.slug, ArtifactComment(id="m1", anchor_orphaned=True))
        raw = (store.root / art.slug / "comments.json").read_text(encoding="utf-8")
        assert raw == (
            "[\n"
            "  {\n"
            '    "id": "m1",\n'
            '    "origin": "local",\n'
            '    "scope": "private",\n'
            '    "author": "",\n'
            '    "is_agent": false,\n'
            '    "body": "",\n'
            '    "thread_id": "",\n'
            '    "status": "open",\n'
            '    "sync_state": "local_only",\n'
            '    "created_at": "",\n'
            '    "updated_at": "",\n'
            '    "anchor_orphaned": true\n'
            "  }\n"
            "]"
        )
        # An empty thread_id reloads as the comment's own id (a root).
        [loaded] = store.list_comments(art.slug)
        assert loaded.thread_id == "m1"
        assert loaded.anchor_orphaned is True
        assert loaded.deleted is False

    def test_folder_file_bytes(self, tmp_path: Path, monkeypatch) -> None:
        counter = iter(range(1, 100))
        monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(int=next(counter) << 96))
        fs = ArtifactFolderStore(path=tmp_path / "artifact_folders.json")
        a = fs.create("Reports", color="#AABBCC")
        b = fs.create("Q3", parent_id=a["id"])
        fs.set_icon(b["id"], "\U0001f4ca")
        leaf = fs.resolve_path("Reports/Q3/Deep", create_missing=True)
        raw = (tmp_path / "artifact_folders.json").read_text(encoding="utf-8")
        assert raw == (
            "[\n"
            "  {\n"
            '    "color": "#aabbcc",\n'
            '    "id": "000000010000",\n'
            '    "name": "Reports",\n'
            '    "order": 0,\n'
            '    "parent_id": ""\n'
            "  },\n"
            "  {\n"
            '    "icon": "\\ud83d\\udcca",\n'
            '    "id": "000000020000",\n'
            '    "name": "Q3",\n'
            '    "order": 1,\n'
            '    "parent_id": "000000010000"\n'
            "  },\n"
            "  {\n"
            '    "id": "000000030000",\n'
            '    "name": "Deep",\n'
            '    "order": 2,\n'
            '    "parent_id": "000000020000"\n'
            "  }\n"
            "]"
        )
        assert fs.breadcrumb(leaf) == "Reports/Q3/Deep"
        assert fs.breadcrumb(leaf, sep=" › ") == "Reports › Q3 › Deep"
        # The rename epoch is returned from inside the bump; each rename advances it.
        assert fs.rename(b["id"], "Q4")[1] == 2
        assert fs.rename(b["id"], "Q5")[1] == 3
        assert fs.set_icon_if_epoch(b["id"], "x", 2) is None
        assert fs.set_icon_if_epoch(b["id"], "x", 3)["icon"] == "x"
        # A second store over the same file reloads the same records.
        again = ArtifactFolderStore(path=tmp_path / "artifact_folders.json")
        assert [f["id"] for f in again.list()] == ["000000010000", "000000020000", "000000030000"]

    def test_layout_after_create_snapshot_and_image(self, store: ArtifactStore) -> None:
        art = store.create(name="Doc", content="v1")
        store.update(art.slug, content="v2", snapshot=True)
        store.update(art.slug, content="v3")
        adir = store.root / art.slug
        assert sorted(p.name for p in adir.iterdir()) == ["current.html", "meta.json", "versions"]
        assert sorted(p.name for p in (adir / "versions").iterdir()) == ["v1.html", "v2.html"]
        assert (adir / "current.html").read_text(encoding="utf-8") == "v3"
        assert (adir / "versions" / "v2.html").read_text(encoding="utf-8") == "v2"

        img = store.create_image(name="Shot", image_bytes=_png(), mime="IMAGE/PNG")
        idir = store.root / img.slug
        assert sorted(p.name for p in idir.iterdir()) == [
            "asset.png",
            "current.html",
            "meta.json",
            "versions",
        ]
        assert (idir / "current.html").read_text(encoding="utf-8") == ""
        meta = json.loads((idir / "meta.json").read_text(encoding="utf-8"))
        assert meta["image"] == {
            "alt": "",
            "ext": "png",
            "height": 2,
            "mime": "image/png",
            "original_filename": "",
            "sha256": meta["image"]["sha256"],
            "size_bytes": len(_png()),
            "width": 3,
        }
        assert store.read_image_bytes(img.slug) == (_png(), "image/png")


class TestResponseShape:
    """Dataclass field order is the key order of every serialized artifact."""

    def test_field_order(self) -> None:
        def names(cls: type) -> list[str]:
            return [f.name for f in dataclasses.fields(cls)]

        assert names(Artifact) == [
            "slug",
            "name",
            "kind",
            "kind_auto",
            "source",
            "description",
            "tags",
            "version",
            "created_at",
            "updated_at",
            "content",
            "events",
            "events_backfilled",
            "source_path",
            "source_root",
            "source_copy_only",
            "folder_id",
            "pinned",
            "session_key",
            "auto_registered",
            "publication",
            "fork_metadata",
            "version_kinds",
            "live_dirty",
            "source_missing",
            "slug_collided_with",
            "webapp_metadata",
            "image",
            "content_token",
        ]
        assert names(ArtifactPublication) == [
            "artifact_id",
            "view_url",
            "provider",
            "visibility",
            "shared_with",
            "auto_sync",
            "collab_mode",
            "last_pushed_sha256",
            "last_synced_kirocrew_version",
            "wrapper_revision",
            "version_map",
            "published_at",
            "published_by",
            "last_error",
            "notice",
            "notice_code",
            "last_synced_remote_hash",
        ]
        assert names(ForkMetadata) == [
            "upstream_artifact_id",
            "upstream_url",
            "upstream_owner",
            "upstream_version",
            "forked_at",
            "upstream_provider",
        ]
        assert names(ImageMetadata) == [
            "mime",
            "ext",
            "size_bytes",
            "width",
            "height",
            "sha256",
            "original_filename",
            "alt",
        ]
        assert names(ArtifactComment)[-3:] == ["created_at", "updated_at", "deleted"]

    def test_to_dict_projections(self, store: ArtifactStore) -> None:
        art = store.create(name="x", content="y")
        plain = list(art.to_dict())
        assert "content" not in plain and "slug_collided_with" not in plain
        assert plain[-4:] == ["live_dirty", "source_missing", "webapp_metadata", "image"]
        with_content = list(art.to_dict(include_content=True))
        assert with_content.index("content") == with_content.index("updated_at") + 1
        persisted = art.to_dict(persist=True)
        assert "live_dirty" not in persisted and "source_missing" not in persisted
        assert "content" not in persisted


class TestTolerantLoad:
    def test_unknown_and_malformed_keys(self, store: ArtifactStore) -> None:
        adir = store.root / "legacy"
        (adir / "versions").mkdir(parents=True)
        (adir / "current.html").write_text("body", encoding="utf-8")
        (adir / "meta.json").write_text(
            json.dumps(
                {
                    "slug": "legacy",
                    "future_key": {"nested": True},
                    "publication": {"view_url": "no-id"},
                    "fork_metadata": {"upstream_artifact_id": "u1", "upstream_version": "x"},
                    "image": {"mime": 7, "width": True, "height": 4, "size_bytes": "9"},
                    "version_kinds": {"1": "html", "2": 3, 4: "svg"},
                    "events": ["bad", {"type": "created"}],
                }
            ),
            encoding="utf-8",
        )
        art = store.get("legacy")
        assert art.name == "legacy"
        assert art.kind == "widget"
        assert art.publication is None
        assert art.fork_metadata == ForkMetadata(upstream_artifact_id="u1", upstream_version=0)
        assert art.image == ImageMetadata(height=4)
        assert art.version_kinds == {"1": "html", "4": "svg"}
        assert art.events == [{"type": "created"}]
        assert art.content == "body"

    def test_publication_defaults(self, store: ArtifactStore) -> None:
        art = store.create(name="p", content="c")
        meta_path = store.root / art.slug / "meta.json"
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
        raw["publication"] = {
            "artifact_id": "a1",
            "collab_mode": "weird",
            "last_synced_kirocrew_version": "n/a",
            "version_map": {"1": "2", "2": "x"},
            "shared_with": ["bob", 3],
        }
        meta_path.write_text(json.dumps(raw), encoding="utf-8")
        pub = store.get(art.slug).publication
        assert pub is not None
        assert pub.collab_mode == "mirror"
        assert pub.last_synced_kirocrew_version == 0
        assert pub.version_map == {"1": 2}
        assert pub.shared_with == ["bob"]
        assert pub.visibility == "PRIVATE"
        assert pub.auto_sync is True


class TestErrorMessages:
    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"name": ""}, "name is required"),
            ({"name": 3}, "name must be str, got int"),
            ({"name": "x" * 201}, "name exceeds 200 chars"),
            ({"description": "x" * 2001}, "description exceeds 2000 chars"),
            ({"tags": "ops"}, "tags must be a list, got str"),
            ({"tags": ["t"] * 17}, "too many tags (17 > 16)"),
            ({"tags": ["-bad"]}, "invalid tag '-bad': a tag must start with a letter or digit"),
            ({"kind": "movie"}, "invalid kind 'movie': must be one of"),
            ({"source": "fax"}, "invalid source 'fax': must be one of"),
            ({"slug": "Bad Slug"}, "invalid slug 'Bad Slug': must match"),
            (
                {"source_path": "/p" * 300},
                "source_path exceeds 512 chars (600); refusing to truncate",
            ),
        ],
    )
    def test_create_validation_messages(self, store: ArtifactStore, kwargs, message) -> None:
        base = {"name": "ok", "content": "c"}
        base.update(kwargs)
        with pytest.raises(ArtifactValidationError) as info:
            store.create(**base)
        assert str(info.value).startswith(message)

    def test_content_cap_follows_the_facade_constant(
        self, store: ArtifactStore, monkeypatch
    ) -> None:
        monkeypatch.setattr(art_mod, "MAX_CONTENT_BYTES", 10)
        with pytest.raises(ArtifactValidationError, match=r"^content exceeds 10 bytes \(11\)$"):
            store.create(name="big", content="x" * 11)

    def test_explicit_slug_collision(self, store: ArtifactStore) -> None:
        store.create(name="a", content="c", slug="taken")
        with pytest.raises(ArtifactAlreadyExistsError, match=r"^artifact already exists: taken$"):
            store.create(name="b", content="c", slug="taken")
        derived = store.create(name="taken", content="c")
        assert (derived.slug, derived.slug_collided_with) == ("taken-2", "taken")

    def test_generation_tokens(self, store: ArtifactStore) -> None:
        art = store.create(name="gen", content="c")
        with pytest.raises(ArtifactReplacedError) as info:
            store.delete(art.slug, expect_created_at="other")
        assert str(info.value) == (
            f"artifact gen was created at {art.created_at!r}, not 'other': the artifact under "
            "this slug was replaced, so deleting it would destroy one nobody asked to delete"
        )
        with pytest.raises(ArtifactReplacedError, match=r"caller read this slug as empty"):
            store.delete(art.slug, expect_created_at=EXPECT_ABSENT)
        store.set_publication(art.slug, ArtifactPublication(artifact_id="pub-1", view_url="u"))
        with pytest.raises(ArtifactStillPublishedError, match=r"^artifact gen is still published"):
            store.delete(art.slug, refuse_if_published=True)
        with pytest.raises(ArtifactReplacedError, match=r"is published as 'pub-1', not 'pub-2'"):
            store.clear_publication(art.slug, expect_publication_id="pub-2")
        cleared = store.clear_publication(
            art.slug, expect_created_at=art.created_at, expect_publication_id="pub-1"
        )
        assert cleared.publication is None
        store.delete(art.slug, refuse_if_published=True, expect_created_at=art.created_at)
        with pytest.raises(ArtifactNotFoundError, match=r"^artifact not found: gen$"):
            store.delete(art.slug)

    def test_invalid_event_type_leaves_no_version_file(self, store: ArtifactStore) -> None:
        art = store.create(name="ev", content="c")
        with pytest.raises(ArtifactValidationError, match=r"^invalid event type 'bogus'"):
            store.update(art.slug, content="d", snapshot=True, event_type="bogus")
        assert not (store.root / art.slug / "versions" / "v2.html").exists()
        assert store.get(art.slug).version == 1

    def test_patch_allowlists(self, store: ArtifactStore) -> None:
        art = store.create(name="al", content="c")
        with pytest.raises(ArtifactValidationError, match=r"is not published; cannot update"):
            store.update_publication(art.slug, notice="x")
        store.set_publication(art.slug, ArtifactPublication(artifact_id="p", view_url="u"))
        with pytest.raises(ArtifactValidationError, match=r"^unknown publication field: bogus$"):
            store.update_publication(art.slug, bogus=1)
        with pytest.raises(ArtifactError, match=r"has no fork_metadata"):
            store.update_fork_metadata(art.slug, upstream_url="x")
        store.set_fork_metadata(art.slug, ForkMetadata(upstream_artifact_id="u"))
        with pytest.raises(
            ArtifactError, match=r"^ForkMetadata.upstream_version expects int, got str$"
        ):
            store.update_fork_metadata(art.slug, upstream_version="1")
        store.add_comment(art.slug, ArtifactComment(id="k"))
        with pytest.raises(ArtifactError, match=r"^comment field 'author' is not patchable$"):
            store.update_comment(art.slug, "k", author="x")


class TestChangeListenerOrdering:
    def test_actions_fire_outside_the_lock_before_the_counter(
        self, store: ArtifactStore, monkeypatch
    ) -> None:
        calls: list[tuple[str, ...]] = []

        def listener(action: str, slug: str) -> None:
            # A listener may call back into the store, so the lock must be free.
            assert not store._lock.locked()
            calls.append(("listener", action, slug))

        monkeypatch.setattr(
            art_mod, "emit_counter", lambda name, attrs: calls.append(("counter", name))
        )
        store.set_change_listener(listener)
        art = store.create(name="L", content="c")
        store.update(art.slug, description="metadata only")
        store.update(art.slug, name="Renamed")
        store.update(art.slug, kind="json")
        store.update(art.slug, content="new")
        store.delete(art.slug)
        assert calls == [
            ("listener", "upsert", "l"),
            ("counter", art_mod.ARTIFACTS_CREATED),
            ("listener", "rename", "l"),
            ("listener", "upsert", "l"),
            ("listener", "upsert", "l"),
            ("listener", "delete", "l"),
        ]

    def test_listener_failure_is_swallowed(self, store: ArtifactStore, caplog) -> None:
        def boom(action: str, slug: str) -> None:
            raise RuntimeError("listener down")

        store.set_change_listener(boom)
        with caplog.at_level("ERROR", logger="kiro_crew.artifacts"):
            store.create(name="swallow", content="c")
        assert store.get("swallow").content == "c"
        assert [
            r.getMessage()
            for r in caplog.records
            if r.name == "kiro_crew.artifacts" and r.levelname == "ERROR"
        ] == ["artifact change listener failed: action=upsert slug=swallow"]


#: The moved classes that are not errors: each reports the module that defines it.
_MOVED_RECORDS = (
    Artifact,
    ArtifactComment,
    ArtifactPublication,
    ForkMetadata,
    ImageMetadata,
    type(EXPECT_ABSENT),
    ArtifactFolderStore,
)


class TestFacadeSeams:
    """Names callers and tests patch on :mod:`kiro_crew.artifacts` keep steering the store."""

    def test_timestamps_follow_the_facade_clock(self, store: ArtifactStore, monkeypatch) -> None:
        art = store.create(name="t", content="c")
        later = "2027-01-01T00:00:00.000001+00:00"
        monkeypatch.setattr(art_mod, "_now_iso", lambda: later)
        store.update(art.slug, content="d", snapshot=True)
        store.add_comment(art.slug, ArtifactComment(id="a", anchor_quote="missing"))
        store.update(art.slug, content="e")
        fresh = store.get(art.slug)
        assert fresh.created_at == NOW
        assert fresh.updated_at == later
        assert fresh.events[-1]["ts"] == later
        [comment] = store.list_comments(art.slug)
        assert comment.anchor_orphaned is True
        assert comment.updated_at == later

    def test_version_cap_follows_the_facade_constant(
        self, store: ArtifactStore, monkeypatch
    ) -> None:
        monkeypatch.setattr(art_mod, "MAX_VERSIONS", 2)
        art = store.create(name="cap", content="1")
        for body in ("2", "3", "4"):
            store.update(art.slug, content=body, snapshot=True)
        assert store.list_versions(art.slug) == [3, 4]

    def test_comment_cap_follows_the_facade_constant(
        self, store: ArtifactStore, monkeypatch
    ) -> None:
        monkeypatch.setattr(art_mod, "MAX_COMMENTS_PER_ARTIFACT", 2)
        art = store.create(name="cc", content="c")
        for n in range(3):
            store.add_comment(art.slug, ArtifactComment(id=f"r{n}"))
        assert [c.id for c in store.list_comments(art.slug)] == ["r1", "r2"]

    def test_default_store_singletons(self, tmp_path: Path, monkeypatch) -> None:
        mine = ArtifactStore(root=tmp_path / "a")
        folders = ArtifactFolderStore(path=tmp_path / "f.json")
        monkeypatch.setattr(art_mod, "_default_store", mine)
        monkeypatch.setattr(art_mod, "_default_folder_store", folders)
        assert art_mod.get_default_store() is mine
        assert art_mod.get_default_folder_store() is folders

    def test_default_store_is_built_once(self, tmp_path: Path, monkeypatch) -> None:
        # Hold the one build open until every caller has reached the singleton's
        # lock, so the test observes concurrent first calls rather than hoping for
        # them.
        real_lock = threading.Lock()
        entered: list[int] = []
        builds: list[int] = []
        release = threading.Event()

        class CountingLock:
            def __enter__(self) -> "CountingLock":
                entered.append(1)
                real_lock.acquire()
                return self

            def __exit__(self, *exc: object) -> None:
                real_lock.release()

        class GatedStore(ArtifactStore):
            def __init__(self, root: Path | None = None) -> None:
                builds.append(1)
                release.wait(timeout=10)
                super().__init__(root=tmp_path / "artifacts")

        monkeypatch.setattr(art_mod, "_default_store", None)
        monkeypatch.setattr(art_mod, "_default_store_lock", CountingLock())
        monkeypatch.setattr(art_mod, "ArtifactStore", GatedStore)
        gate = threading.Barrier(4, timeout=10)
        seen: list[ArtifactStore] = []

        def build() -> None:
            gate.wait()
            seen.append(art_mod.get_default_store())

        threads = [threading.Thread(target=build) for _ in range(4)]
        for t in threads:
            t.start()
        deadline = time.monotonic() + 10
        while len(entered) < 4 and len(builds) < 4 and time.monotonic() < deadline:
            time.sleep(0.005)
        release.set()
        for t in threads:
            t.join(timeout=10)
        assert len(builds) == 1
        assert len(seen) == 4 and len({id(s) for s in seen}) == 1

    def test_default_store_root(self, monkeypatch) -> None:
        monkeypatch.setattr(art_mod, "_default_store", None)
        assert art_mod.get_default_store().root == art_mod.config_dir() / "artifacts"

    def test_facade_config_dir_moves_the_default_folder_store(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        home = tmp_path / "home"
        monkeypatch.setattr(art_mod, "config_dir", lambda: home)
        monkeypatch.setattr(art_mod, "_default_folder_store", None)
        monkeypatch.setattr(art_mod, "_default_store", None)
        assert art_mod.get_default_folder_store()._path == home / "artifact_folders.json"
        assert art_mod.get_default_store().root == home / "artifacts"

    def test_event_cap_follows_the_facade_constant(self, store: ArtifactStore, monkeypatch) -> None:
        monkeypatch.setattr(art_mod, "MAX_EVENTS_PER_ARTIFACT", 2)
        art = store.create(name="ev-cap", content="1")
        for body in ("2", "3", "4"):
            store.update(art.slug, content=body, snapshot=True)
        assert [e["version"] for e in store.get(art.slug).events] == [3, 4]

    def test_moved_errors_report_the_facade_module(self) -> None:
        for cls in (
            ArtifactError,
            ArtifactNotFoundError,
            ArtifactAlreadyExistsError,
            ArtifactValidationError,
            ArtifactStillPublishedError,
            ArtifactReplacedError,
        ):
            assert cls.__module__ == "kiro_crew.artifacts", cls

    def test_moved_records_report_their_owner_module(self) -> None:
        owners = {cls: cls.__module__ for cls in _MOVED_RECORDS}
        assert owners == {
            Artifact: "kiro_crew.artifact_store.model",
            ArtifactComment: "kiro_crew.artifact_store.model",
            ArtifactPublication: "kiro_crew.artifact_store.model",
            ForkMetadata: "kiro_crew.artifact_store.model",
            ImageMetadata: "kiro_crew.artifact_store.model",
            type(EXPECT_ABSENT): "kiro_crew.artifact_store.model",
            ArtifactFolderStore: "kiro_crew.artifact_store.folders",
        }

    @pytest.mark.parametrize("cls", _MOVED_RECORDS, ids=lambda cls: cls.__name__)
    def test_the_source_of_a_moved_record_is_found(self, cls: type) -> None:
        assert f"class {cls.__name__}" in inspect.getsource(cls)


def _fake_infer_kind(content: str, source_path: str = "", explicit: str | None = None) -> str:
    return "json"


def _fake_validate_slug(slug: str) -> str:
    if isinstance(slug, str) and slug.startswith("blocked-"):
        raise ArtifactValidationError(f"blocked slug {slug!r}")
    return slug


def _stored_image_meta(store: ArtifactStore, **kwargs) -> dict:
    img = store.create_image(name="img", image_bytes=_png(), mime="image/png", **kwargs)
    return json.loads((store.root / img.slug / "meta.json").read_text(encoding="utf-8"))["image"]


def _reads_infer_kind(store: ArtifactStore) -> None:
    art = store.create(name="sniffed", content="# hi")
    assert art.kind == "json"
    assert store.get(art.slug).kind == "json"


def _reads_validate_slug(store: ArtifactStore) -> None:
    assert art_mod.slug_is_well_formed("blocked-a") is False
    assert art_mod.slug_is_well_formed("fine-a") is True
    with pytest.raises(ArtifactValidationError, match=r"^blocked slug 'blocked-a'$"):
        store.get("blocked-a")
    with pytest.raises(ArtifactValidationError, match=r"^blocked slug 'blocked-a'$"):
        store.delete("blocked-a")
    with pytest.raises(ArtifactValidationError, match=r"^blocked slug 'blocked-b'$"):
        store.create(name="b", content="c", slug="blocked-b")


def _reads_name_limit(store: ArtifactStore) -> None:
    with pytest.raises(ArtifactValidationError, match=r"^name exceeds 5 chars$"):
        store.create(name="x" * 6, content="c")
    assert _stored_image_meta(store, original_filename="y" * 50)["original_filename"] == "y" * 5


def _reads_description_limit(store: ArtifactStore) -> None:
    with pytest.raises(ArtifactValidationError, match=r"^description exceeds 5 chars$"):
        store.create(name="d", content="c", description="d" * 6)
    assert _stored_image_meta(store, alt="a" * 50)["alt"] == "a" * 5


def _reads_event_types(store: ArtifactStore) -> None:
    art = store.create(name="ev", content="c")
    store.update(art.slug, content="new", snapshot=True, event_type="probe")
    assert [e["type"] for e in store.get(art.slug).events] == ["created", "probe"]


def _reads_tag_limit(store: ArtifactStore) -> None:
    with pytest.raises(ArtifactValidationError, match=r"^too many tags \(2 > 1\)$"):
        store.create(name="t", content="c", tags=["a", "b"])


def _reads_slug_grammar(store: ArtifactStore) -> None:
    assert art_mod.slug_is_well_formed("fine-a") is True
    assert art_mod.slug_is_well_formed("plain") is False
    with pytest.raises(ArtifactValidationError, match=r"^invalid slug 'plain': must match"):
        store.create(name="p", content="c", slug="plain")


def _reads_folder_depth(store: ArtifactStore) -> None:
    folders = ArtifactFolderStore(path=store.root.parent / "artifact_folders.json")
    with pytest.raises(ArtifactValidationError, match=r"^folder nesting exceeds max depth 1$"):
        folders.resolve_path("a/b", create_missing=True)


def _reads_jpeg_sniffer(store: ArtifactStore) -> None:
    img = store.create_image(name="j", image_bytes=_jpeg(60, 40), mime="image/jpeg")
    assert (img.image.width, img.image.height) == (7, 9)


#: ``(module, attribute, replacement(original), probe)``: the probe drives every
#: reader of the attribute and fails unless each one sees the replacement.
SEAM_CASES = [
    pytest.param(
        "kiro_crew.artifacts",
        "_infer_kind",
        lambda _original: _fake_infer_kind,
        _reads_infer_kind,
        id="facade-_infer_kind",
    ),
    pytest.param(
        "kiro_crew.artifacts",
        "_validate_slug",
        lambda _original: _fake_validate_slug,
        _reads_validate_slug,
        id="facade-_validate_slug",
    ),
    pytest.param(
        "kiro_crew.artifact_store.rules",
        "MAX_NAME_LEN",
        lambda _original: 5,
        _reads_name_limit,
        id="rules-MAX_NAME_LEN",
    ),
    pytest.param(
        "kiro_crew.artifact_store.rules",
        "MAX_DESCRIPTION_LEN",
        lambda _original: 5,
        _reads_description_limit,
        id="rules-MAX_DESCRIPTION_LEN",
    ),
    pytest.param(
        "kiro_crew.artifact_store.records",
        "ALLOWED_EVENT_TYPES",
        lambda original: frozenset(original | {"probe"}),
        _reads_event_types,
        id="records-ALLOWED_EVENT_TYPES",
    ),
    # Rule data the owners read, patched through the facade: the write reaches the owner.
    pytest.param(
        "kiro_crew.artifacts",
        "MAX_TAGS",
        lambda _original: 1,
        _reads_tag_limit,
        id="facade-MAX_TAGS",
    ),
    pytest.param(
        "kiro_crew.artifacts",
        "MAX_NAME_LEN",
        lambda _original: 5,
        _reads_name_limit,
        id="facade-MAX_NAME_LEN",
    ),
    pytest.param(
        "kiro_crew.artifacts",
        "_SLUG_RE",
        lambda _original: re.compile(r"^fine-[a-z]\Z"),
        _reads_slug_grammar,
        id="facade-_SLUG_RE",
    ),
    pytest.param(
        "kiro_crew.artifacts",
        "ALLOWED_EVENT_TYPES",
        lambda original: frozenset(original | {"probe"}),
        _reads_event_types,
        id="facade-ALLOWED_EVENT_TYPES",
    ),
    pytest.param(
        "kiro_crew.artifacts",
        "MAX_FOLDER_DEPTH",
        lambda _original: 1,
        _reads_folder_depth,
        id="facade-MAX_FOLDER_DEPTH",
    ),
    pytest.param(
        "kiro_crew.artifacts",
        "_sniff_jpeg_dimensions",
        lambda _original: lambda _data: (7, 9),
        _reads_jpeg_sniffer,
        id="facade-_sniff_jpeg_dimensions",
    ),
]


class TestSeamReach:
    """One patch of a seam, on the module that holds its live binding, steers every reader."""

    @pytest.mark.parametrize(("module_path", "attr", "replacement", "probe"), SEAM_CASES)
    def test_a_patch_reaches_every_reader(
        self, store: ArtifactStore, monkeypatch, module_path, attr, replacement, probe
    ) -> None:
        # Resolved per case rather than at import, so the facade cases in this file
        # collect and run even on a tree that has no kiro_crew.artifact_store package.
        module = importlib.import_module(module_path)
        monkeypatch.setattr(module, attr, replacement(getattr(module, attr)))
        probe(store)

    def test_every_name_tests_rebind_on_the_facade_reaches_its_readers(self) -> None:
        """Derived from the tests themselves: a name rebound on the facade either is
        forwarded, so the write lands in the owner every reader reads, or is held
        by the facade alone, read by no owner module as its own global."""
        patched = _corpus_patched_names()
        # Non-vacuous: the scan sees the seams the store's own tests rebind.
        assert {"MAX_VERSIONS", "_now_iso", "config_dir", "MAX_TAGS", "_validate_slug"} <= patched
        split: dict[str, list[str]] = {}
        for name in sorted(patched):
            if name in art_mod._EXPORTS:
                assert name not in vars(art_mod), name
                continue
            assert name in vars(art_mod), name
            readers = [leaf for leaf, source in _owner_sources() if name in _bare_reads(source)]
            if readers:
                split[name] = readers
        assert split == _DOCUMENTED_SPLITS

    def test_the_patch_scan_finds_each_spelling(self) -> None:
        source = (
            "from kiro_crew import artifacts as art\n"
            "import kiro_crew.artifacts as facade\n"
            "def test_x(monkeypatch, other):\n"
            '    monkeypatch.setattr(art, "MAX_TAGS", 1)\n'
            '    monkeypatch.setattr(facade, "_SLUG_RE", 1)\n'
            '    monkeypatch.setattr("kiro_crew.artifacts.MAX_NAME_LEN", 1)\n'
            '    mock.patch.object(art, "config_dir")\n'
            '    monkeypatch.setattr(other, "NOT_THE_FACADE", 1)\n'
            '    monkeypatch.setattr(art.ArtifactStore, "create", None)\n'
        )
        assert _facade_patched_names(source) == {
            "MAX_TAGS",
            "_SLUG_RE",
            "MAX_NAME_LEN",
            "config_dir",
        }

    def test_the_reader_scan_counts_only_bare_global_reads(self) -> None:
        source = (
            "def f(limit: MAX_TAGS = 0) -> MAX_NAME_LEN:\n"
            "    return MAX_TAGS + rules.MAX_SOURCE_PATH_LEN\n"
            "def g(MAX_FOLDER_DEPTH):\n"
            "    return MAX_FOLDER_DEPTH\n"
        )
        assert _bare_reads(source) == {"MAX_TAGS", "rules"}


#: Facade-held names an owner also reads through a binding of its own, each a
#: documented contract: a directly constructed ``ArtifactFolderStore`` takes its
#: default path from ``folders``' own ``config_dir``.
_DOCUMENTED_SPLITS = {"config_dir": ["folders"]}

#: Every spelling a test uses to name the facade module.
_MENTIONS_THE_FACADE = re.compile(
    r"kiro_crew\.artifacts\b|from kiro_crew import [^\n]*\bartifacts\b"
)


def _facade_patched_names(text: str) -> set[str]:
    """Names one test file rebinds on ``kiro_crew.artifacts`` itself."""
    tree = ast.parse(text)
    aliases = {"kiro_crew.artifacts"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "kiro_crew":
            aliases |= {a.asname or a.name for a in node.names if a.name == "artifacts"}
        elif isinstance(node, ast.Import):
            aliases |= {
                a.asname for a in node.names if a.name == "kiro_crew.artifacts" and a.asname
            }
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        target, name = node.args[0], node.args[1]
        if (
            ast.unparse(target) in aliases
            and isinstance(name, ast.Constant)
            and isinstance(name.value, str)
            and ast.unparse(node.func).endswith(("setattr", "patch.object"))
        ):
            found.add(name.value)
    found |= set(re.findall(r"""["']kiro_crew\.artifacts\.(\w+)["']""", text))
    return found


def _corpus_patched_names() -> set[str]:
    """Names any test rebinds on the facade, the parametrized seam cases above included."""
    root = repo_root()
    found = {case.values[1] for case in SEAM_CASES if case.values[0] == "kiro_crew.artifacts"}
    for path in repo_files_named(".py"):
        relative = path.relative_to(root).parts
        in_tests = relative[0] == "test" or (relative[0] == "src" and "tests" in relative)
        if not in_tests or not path.name.startswith("test_"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if _MENTIONS_THE_FACADE.search(text):
            found |= _facade_patched_names(text)
    return found


def _owner_sources() -> list[tuple[str, str]]:
    """``(leaf, source)`` for every owner module of the artifact store."""
    package = Path(inspect.getfile(art_mod)).parent / "artifact_store"
    return [
        (path.stem, path.read_text(encoding="utf-8"))
        for path in sorted(package.glob("*.py"))
        if path.stem != "__init__"
    ]


def _bare_reads(source: str) -> set[str]:
    """Names a function in *source* reads as a module global: a bare Load outside
    annotations that the function does not bind itself. Module-level code runs once,
    at import, so it is not a reader a later patch could miss."""
    tree = ast.parse(source)
    annotations: set[int] = set()
    for node in ast.walk(tree):
        for field in ("annotation", "returns"):
            sub = getattr(node, field, None)
            if sub is not None:
                annotations.update(id(part) for part in ast.walk(sub))
    function_types = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
    nested = {
        id(inner)
        for outer in ast.walk(tree)
        if isinstance(outer, function_types)
        for inner in ast.walk(outer)
        if inner is not outer and isinstance(inner, function_types)
    }
    found: set[str] = set()
    for function in ast.walk(tree):
        if not isinstance(function, function_types) or id(function) in nested:
            continue
        local = {arg.arg for arg in ast.walk(function) if isinstance(arg, ast.arg)}
        local |= {
            node.id
            for node in ast.walk(function)
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del))
        }
        found |= {
            node.id
            for node in ast.walk(function)
            if isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id not in local
            and id(node) not in annotations
        }
    return found


#: Every public name the facade bound before its rules moved into
#: :mod:`kiro_crew.artifact_store`, the moved names included. Each must still resolve
#: here and reach a star import.
BASE_PUBLIC_NAMES = frozenset(
    {
        "ALLOWED_EVENT_TYPES",
        "ALLOWED_KINDS",
        "ALLOWED_SOURCES",
        "ARTIFACTS_CREATED",
        "ARTIFACT_MAX_CONTENT_BYTES",
        "Any",
        "Artifact",
        "ArtifactAlreadyExistsError",
        "ArtifactComment",
        "ArtifactConflictError",
        "ArtifactError",
        "ArtifactFolderStore",
        "ArtifactNotFoundError",
        "ArtifactPublication",
        "ArtifactReplacedError",
        "ArtifactStillPublishedError",
        "ArtifactStore",
        "ArtifactValidationError",
        "Callable",
        "DEFAULT_PROVIDER",
        "DOC_EXTENSIONS",
        "EXPECT_ABSENT",
        "FOLDER_PATH_SEP",
        "ForkMetadata",
        "ImageMetadata",
        "Iterator",
        "KiroCrewConfig",
        "MAX_AUTO_WIDGET_ARTIFACTS",
        "MAX_COMMENTS_PER_ARTIFACT",
        "MAX_CONTENT_BYTES",
        "MAX_DESCRIPTION_LEN",
        "MAX_EVENTS_PER_ARTIFACT",
        "MAX_FOLDER_DEPTH",
        "MAX_NAME_LEN",
        "MAX_SOURCE_PATH_LEN",
        "MAX_TAGS",
        "MAX_TAG_LEN",
        "MAX_VERSIONS",
        "Mapping",
        "MappingProxyType",
        "Path",
        "USER_SELECTABLE_KINDS",
        "WebAppArchitecture",
        "WebAppCost",
        "WebAppDeployTarget",
        "WebAppLifecycle",
        "WebAppMetadata",
        "WebAppTeardown",
        "annotations",
        "asdict",
        "canonical_path_refusal",
        "config_dir",
        "dataclass",
        "datetime",
        "detect_editor_kind",
        "emit_counter",
        "field",
        "fields_of",
        "filter_comments_for_forward",
        "get_default_folder_store",
        "get_default_store",
        "has_unthemed_hardcoded_colors",
        "hashlib",
        "hooks",
        "is_document_path",
        "is_sensitive_canonical_path",
        "is_sensitive_path",
        "is_unverifiable_path_refusal",
        "is_verifiable_root",
        "json",
        "logger",
        "logging",
        "normalize_tag",
        "os",
        "pinned_fs",
        "re",
        "sensitive_path_refusal",
        "slug_hash_fallback",
        "slug_is_well_formed",
        "slugify",
        "tempfile",
        "threading",
        "timezone",
        "unicodedata",
        "uuid",
        "webapp_metadata_from_dict",
    }
)

#: Every public name of the facade: a star import of :mod:`kiro_crew.artifacts` binds
#: exactly these, the base names plus ``platform_compat``, a public module binding.
FACADE_PUBLIC_NAMES = BASE_PUBLIC_NAMES | {"platform_compat"}


class TestPublicSurface:
    def test_star_import_exposes_the_same_public_names(self, tmp_path: Path) -> None:
        # A real star import, performed by the import system in a throwaway module.
        probe = tmp_path / "artifacts_star_import_probe.py"
        probe.write_text("from kiro_crew.artifacts import *\n", encoding="utf-8")
        spec = importlib.util.spec_from_file_location("artifacts_star_import_probe", probe)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        ns = {
            n: v for n, v in vars(module).items() if not (n.startswith("__") and n.endswith("__"))
        }
        assert set(ns) == FACADE_PUBLIC_NAMES
        assert [n for n in sorted(ns) if ns[n] is not getattr(art_mod, n)] == []

    def test_every_base_public_name_still_resolves(self) -> None:
        assert [name for name in sorted(BASE_PUBLIC_NAMES) if not hasattr(art_mod, name)] == []

    def test_all_is_derived_from_the_bindings_and_the_forwarding_table(self) -> None:
        bound = set(vars(art_mod)) | set(art_mod._EXPORTS)
        assert art_mod.__all__ == sorted(
            n for n in bound if not n.startswith("_") and n not in art_mod._MACHINERY
        )
        assert sorted(art_mod.__all__) == sorted(FACADE_PUBLIC_NAMES)
        tree = ast.parse(Path(inspect.getfile(art_mod)).read_text(encoding="utf-8"))
        [assigned] = [
            node.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and [ast.unparse(target) for target in node.targets] == ["__all__"]
        ]
        assert not isinstance(assigned, (ast.List, ast.Tuple, ast.Set)), "__all__ is hand-written"


def _jpeg(width: int, height: int) -> bytes:
    # SOI, a fill byte, an APP0 segment, a standalone RST0 and a DHT segment all
    # precede the SOF0 frame header the sniffer has to walk to.
    sof = b"\xff\xc0\x00\x11\x08" + height.to_bytes(2, "big") + width.to_bytes(2, "big")
    return (
        b"\xff\xd8\xff\xff\xff\xe0\x00\x04ab\xff\xd0\xff\xc4\x00\x04zz" + sof + b"\x03" + bytes(10)
    )


def _webp(chunk: bytes, body: bytes) -> bytes:
    return b"RIFF" + bytes(4) + b"WEBP" + chunk + body


def _vp8l(width: int, height: int) -> bytes:
    w, h = width - 1, height - 1
    packed = bytes(
        [w & 0xFF, (w >> 8) & 0x3F | ((h & 0x3) << 6), (h >> 2) & 0xFF, (h >> 10) & 0x0F]
    )
    return _webp(b"VP8L", bytes(4) + b"\x2f" + packed + bytes(5))


def _bmp(width: int, height: int) -> bytes:
    return (
        b"BM"
        + bytes(16)
        + width.to_bytes(4, "little", signed=True)
        + height.to_bytes(4, "little", signed=True)
    )


SNIFF_CASES = [
    ("png", lambda: _png(), "image/png", (3, 2)),
    ("png-truncated", lambda: _png()[:20], "image/png", (None, None)),
    (
        "gif",
        lambda: b"GIF89a" + (7).to_bytes(2, "little") + (5).to_bytes(2, "little"),
        "image/gif",
        (7, 5),
    ),
    ("gif-short", lambda: b"GIF87a\x01", "image/gif", (None, None)),
    ("jpeg", lambda: _jpeg(60, 40), "image/jpeg", (60, 40)),
    (
        "jpeg-bad-length",
        lambda: b"\xff\xd8\xff\xe0\x00\x01" + bytes(10),
        "image/jpeg",
        (None, None),
    ),
    (
        "jpeg-noise-before-frame",
        lambda: b"\xff\xd8\x00\x11\xff\xc0\x00\x11\x08"
        + (9).to_bytes(2, "big")
        + (8).to_bytes(2, "big")
        + bytes(4),
        "image/jpeg",
        (8, 9),
    ),
    ("jpeg-soi-only", lambda: b"\xff\xd8", "image/jpeg", (None, None)),
    ("jpeg-no-signature", lambda: b"notjpeg!!!!", "image/jpeg", (None, None)),
    (
        "webp-vp8",
        lambda: _webp(
            b"VP8 ",
            bytes(7) + b"\x9d\x01\x2a" + (100).to_bytes(2, "little") + (50).to_bytes(2, "little"),
        ),
        "image/webp",
        (100, 50),
    ),
    ("webp-vp8l", lambda: _vp8l(17, 9), "image/webp", (17, 9)),
    (
        "webp-vp8x",
        lambda: _webp(
            b"VP8X", bytes(8) + (299).to_bytes(3, "little") + (199).to_bytes(3, "little")
        ),
        "image/webp",
        (300, 200),
    ),
    ("webp-other-chunk", lambda: _webp(b"ALPH", bytes(14)), "image/webp", (None, None)),
    ("webp-no-signature", lambda: b"RIFF" * 8, "image/webp", (None, None)),
    ("bmp-top-down", lambda: _bmp(12, -34), "image/bmp", (12, 34)),
    ("bmp-zero-width", lambda: _bmp(0, 5), "image/bmp", (None, None)),
    ("unsupported-mime", lambda: _png(), "image/tiff", (None, None)),
]


class TestImageSniffing:
    @pytest.mark.parametrize(
        ("build", "mime", "expected"),
        [
            pytest.param(build, mime, expected, id=name)
            for name, build, mime, expected in SNIFF_CASES
        ],
    )
    def test_header_dimensions(self, build, mime, expected) -> None:
        assert art_mod._sniff_image_dimensions(build(), mime) == expected

    def test_mime_allowlist(self) -> None:
        assert art_mod._IMAGE_MIME_EXT == {
            "image/png": "png",
            "image/jpeg": "jpg",
            "image/webp": "webp",
            "image/gif": "gif",
            "image/bmp": "bmp",
        }

    def test_create_image_refusals(self, store: ArtifactStore, monkeypatch) -> None:
        with pytest.raises(ArtifactValidationError, match=r"^image_bytes must be bytes, got str$"):
            store.create_image(name="x", image_bytes="png", mime="image/png")
        with pytest.raises(
            ArtifactValidationError, match=r"^unsupported image mime 'image/svg\+xml'"
        ):
            store.create_image(name="x", image_bytes=_png(), mime="image/svg+xml")
        with pytest.raises(ArtifactValidationError, match=r"^image bytes are empty$"):
            store.create_image(name="x", image_bytes=b"", mime="image/png")
        monkeypatch.setattr(art_mod, "MAX_CONTENT_BYTES", 10)
        with pytest.raises(ArtifactValidationError, match=r"^image exceeds 10 bytes \(24\)$"):
            store.create_image(name="x", image_bytes=_png(), mime="image/png")
        assert list(store.root.iterdir()) == []

    def test_read_refuses_a_mime_outside_the_allowlist(self, store: ArtifactStore) -> None:
        img = store.create_image(
            name="Pic",
            image_bytes=_jpeg(4, 3),
            mime="image/jpeg",
            alt="a",
            original_filename="p.jpg",
        )
        assert (img.image.width, img.image.height, img.image.ext) == (4, 3, "jpg")
        meta_path = store.root / img.slug / "meta.json"
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
        raw["image"]["mime"] = "text/html"
        meta_path.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(ArtifactNotFoundError, match=r"has an unsupported mime 'text/html'$"):
            store.read_image_bytes(img.slug)
        text = store.create(name="plain", content="c")
        with pytest.raises(ArtifactNotFoundError, match=r"has no image asset$"):
            store.read_image_bytes(text.slug)
