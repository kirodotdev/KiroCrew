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

import dataclasses
import importlib
import importlib.util
import json
import threading
import time
import uuid
from pathlib import Path

import pytest

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


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> ArtifactStore:
    monkeypatch.setattr(art_mod, "_now_iso", lambda: NOW)
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
            (
                {"tags": ["-bad"]},
                "invalid tag '-bad': must match ^[a-zA-Z0-9][a-zA-Z0-9_:.-]{0,63}\\Z",
            ),
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

    def test_moved_classes_report_the_facade_module(self) -> None:
        for cls in (
            Artifact,
            ArtifactComment,
            ArtifactError,
            ArtifactFolderStore,
            ArtifactNotFoundError,
            ArtifactPublication,
            ArtifactReplacedError,
            ForkMetadata,
            ImageMetadata,
            type(EXPECT_ABSENT),
        ):
            assert cls.__module__ == "kiro_crew.artifacts", cls


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


#: Every public name of the facade: a star import of :mod:`kiro_crew.artifacts` binds
#: exactly these, the names its owner modules define included.
FACADE_PUBLIC_NAMES = frozenset(
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
        assert [n for n in sorted(FACADE_PUBLIC_NAMES) if ns[n] is not getattr(art_mod, n)] == []

    def test_all_declares_the_same_public_names(self) -> None:
        assert sorted(art_mod.__all__) == sorted(FACADE_PUBLIC_NAMES)


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
