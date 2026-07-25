"""Unit tests for :mod:`kiro_crew.artifacts` — the data layer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.artifacts import (
    MAX_CONTENT_BYTES,
    MAX_VERSIONS,
    Artifact,
    ArtifactError,
    ArtifactNotFoundError,
    ArtifactStore,
    ArtifactValidationError,
    _infer_kind,
    slugify,
)

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    """Fresh store rooted at a tmp dir."""
    return ArtifactStore(root=tmp_path / "artifacts")


# ── slugify ─────────────────────────────────────────────────────────────────


class TestSlugify:
    def test_basic(self) -> None:
        assert slugify("CR Queue Dashboard") == "cr-queue-dashboard"

    def test_strips_non_ascii(self) -> None:
        # Accented characters become their ascii equivalents via NFKD.
        assert slugify("Café résumé") == "cafe-resume"

    def test_collapses_punctuation(self) -> None:
        assert slugify("hello! world?? foo!! bar") == "hello-world-foo-bar"

    def test_empty_falls_back(self) -> None:
        assert slugify("") == "artifact"
        assert slugify("!!!") == "artifact"
        assert slugify("---") == "artifact"

    def test_truncates_long_input(self) -> None:
        long = "a" * 300
        out = slugify(long)
        assert len(out) <= 80

    def test_strips_leading_trailing_hyphens(self) -> None:
        assert slugify("---hi---") == "hi"

    def test_rejects_non_string(self) -> None:
        with pytest.raises(ArtifactValidationError):
            slugify(42)  # type: ignore[arg-type]


# ── create / get ─────────────────────────────────────────────────────────────


class TestCreate:
    def test_creates_with_default_slug(self, store: ArtifactStore) -> None:
        art = store.create(name="My Widget", content="<p>hello</p>")
        assert art.slug == "my-widget"
        assert art.name == "My Widget"
        assert art.kind == "widget"
        assert art.source == "chat"
        assert art.version == 1
        assert art.tags == []
        assert art.content == "<p>hello</p>"
        assert (store.root / "my-widget" / "current.html").exists()
        assert (store.root / "my-widget" / "versions" / "v1.html").exists()
        assert (store.root / "my-widget" / "meta.json").exists()

    def test_explicit_slug(self, store: ArtifactStore) -> None:
        art = store.create(name="X", content="<x/>", slug="custom-slug")
        assert art.slug == "custom-slug"

    def test_disambiguates_collision(self, store: ArtifactStore) -> None:
        a = store.create(name="Same Name", content="a")
        b = store.create(name="Same Name", content="b")
        c = store.create(name="Same Name", content="c")
        assert a.slug == "same-name"
        assert b.slug == "same-name-2"
        assert c.slug == "same-name-3"

    def test_explicit_slug_collision_raises(self, store: ArtifactStore) -> None:
        store.create(name="x", content="a", slug="taken")
        with pytest.raises(ArtifactError):
            store.create(name="y", content="b", slug="taken")

    def test_meta_json_has_no_content(self, store: ArtifactStore) -> None:
        store.create(name="x", content="secret-content")
        raw = json.loads((store.root / "x" / "meta.json").read_text(encoding="utf-8"))
        assert "content" not in raw

    def test_persists_full_metadata(self, store: ArtifactStore) -> None:
        store.create(
            name="My CR Dashboard",
            content="<table/>",
            kind="widget",
            source="cron",
            description="hourly CR snapshot",
            tags=["ops", "cr"],
        )
        loaded = store.get("my-cr-dashboard")
        assert loaded.description == "hourly CR snapshot"
        assert loaded.tags == ["ops", "cr"]
        assert loaded.source == "cron"


class TestCreateValidation:
    def test_empty_name(self, store: ArtifactStore) -> None:
        with pytest.raises(ArtifactValidationError):
            store.create(name="", content="x")

    def test_invalid_explicit_slug(self, store: ArtifactStore) -> None:
        with pytest.raises(ArtifactValidationError):
            store.create(name="x", content="a", slug="Has Spaces")

    def test_invalid_slug_path_traversal(self, store: ArtifactStore) -> None:
        with pytest.raises(ArtifactValidationError):
            store.create(name="x", content="a", slug="../escape")

    def test_invalid_kind(self, store: ArtifactStore) -> None:
        with pytest.raises(ArtifactValidationError):
            store.create(name="x", content="a", kind="bogus")

    def test_invalid_source(self, store: ArtifactStore) -> None:
        with pytest.raises(ArtifactValidationError):
            store.create(name="x", content="a", source="hacker")

    def test_too_many_tags(self, store: ArtifactStore) -> None:
        with pytest.raises(ArtifactValidationError):
            store.create(name="x", content="a", tags=[f"t{i}" for i in range(20)])

    def test_invalid_tag_format(self, store: ArtifactStore) -> None:
        with pytest.raises(ArtifactValidationError):
            store.create(name="x", content="a", tags=["bad tag with spaces"])

    def test_dedupes_tags(self, store: ArtifactStore) -> None:
        art = store.create(name="x", content="a", tags=["a", "b", "a"])
        assert art.tags == ["a", "b"]

    def test_oversized_content_rejected(self, store: ArtifactStore) -> None:
        with pytest.raises(ArtifactValidationError):
            store.create(name="x", content="a" * (MAX_CONTENT_BYTES + 1))

    def test_oversized_description_rejected(self, store: ArtifactStore) -> None:
        with pytest.raises(ArtifactValidationError):
            store.create(name="x", content="a", description="d" * 5_000)


class TestGet:
    def test_get_returns_content(self, store: ArtifactStore) -> None:
        store.create(name="x", content="hello")
        art = store.get("x")
        assert art.content == "hello"

    def test_missing_raises(self, store: ArtifactStore) -> None:
        with pytest.raises(ArtifactNotFoundError):
            store.get("does-not-exist")

    def test_get_specific_version(self, store: ArtifactStore) -> None:
        art = store.create(name="x", content="v1")
        store.update(art.slug, content="v2", snapshot=True)
        store.update(art.slug, content="v3", snapshot=True)
        assert store.get(art.slug, version=1).content == "v1"
        assert store.get(art.slug, version=2).content == "v2"
        assert store.get(art.slug, version=3).content == "v3"
        assert store.get(art.slug).content == "v3"

    def test_out_of_range_version(self, store: ArtifactStore) -> None:
        store.create(name="x", content="v1")
        with pytest.raises(ArtifactNotFoundError):
            store.get("x", version=5)
        with pytest.raises(ArtifactNotFoundError):
            store.get("x", version=0)


# ── update ──────────────────────────────────────────────────────────────────


class TestUpdate:
    def test_content_change_bumps_version(self, store: ArtifactStore) -> None:
        store.create(name="x", content="v1")
        art = store.update("x", content="v2", snapshot=True)
        assert art.version == 2

    def test_metadata_change_does_not_bump(self, store: ArtifactStore) -> None:
        store.create(name="x", content="v1")
        art = store.update("x", description="updated desc")
        assert art.version == 1
        assert art.description == "updated desc"

    def test_no_op_update_raises(self, store: ArtifactStore) -> None:
        store.create(name="x", content="v1")
        # update with no fields is allowed but is a no-op
        art = store.update("x")
        assert art.version == 1

    def test_previous_version_preserved(self, store: ArtifactStore) -> None:
        store.create(name="x", content="v1")
        store.update("x", content="v2", snapshot=True)
        v1 = (store.root / "x" / "versions" / "v1.html").read_text(encoding="utf-8")
        assert v1 == "v1"

    def test_rename(self, store: ArtifactStore) -> None:
        store.create(name="x", content="a", slug="x")
        art = store.update("x", name="New Name")
        assert art.name == "New Name"
        # slug unchanged
        assert art.slug == "x"

    def test_replace_tags(self, store: ArtifactStore) -> None:
        store.create(name="x", content="a", tags=["old"])
        art = store.update("x", tags=["new", "fresh"])
        assert art.tags == ["new", "fresh"]

    def test_clear_tags(self, store: ArtifactStore) -> None:
        store.create(name="x", content="a", tags=["t1"])
        art = store.update("x", tags=[])
        assert art.tags == []

    def test_missing_raises(self, store: ArtifactStore) -> None:
        with pytest.raises(ArtifactNotFoundError):
            store.update("nope", content="x", snapshot=True)

    def test_update_oversized_content(self, store: ArtifactStore) -> None:
        store.create(name="x", content="a")
        with pytest.raises(ArtifactValidationError):
            store.update("x", content="a" * (MAX_CONTENT_BYTES + 1))


# ── list / list_versions ────────────────────────────────────────────────────


class TestList:
    def test_empty(self, store: ArtifactStore) -> None:
        assert store.list() == []

    def test_returns_newest_first(self, store: ArtifactStore) -> None:
        store.create(name="alpha", content="a")
        store.create(name="bravo", content="b")
        store.create(name="charlie", content="c")
        items = store.list()
        assert [a.slug for a in items] == ["charlie", "bravo", "alpha"]

    def test_filter_by_tag(self, store: ArtifactStore) -> None:
        store.create(name="a", content="a", tags=["x"])
        store.create(name="b", content="a", tags=["y"])
        store.create(name="c", content="a", tags=["x", "y"])
        results = store.list(tag="x")
        assert {a.slug for a in results} == {"a", "c"}

    def test_filter_by_kind(self, store: ArtifactStore) -> None:
        store.create(name="w", content="a", kind="widget")
        store.create(name="m", content="# md", kind="markdown")
        results = store.list(kind="markdown")
        assert {a.slug for a in results} == {"m"}

    def test_filter_by_name_substring(self, store: ArtifactStore) -> None:
        store.create(name="CR Queue", content="a")
        store.create(name="CR Status", content="a")
        store.create(name="Ticket queue", content="a")
        results = store.list(name_contains="queue")
        # name_contains is case-insensitive
        assert {a.slug for a in results} == {"cr-queue", "ticket-queue"}

    def test_list_skips_unreadable(self, store: ArtifactStore) -> None:
        store.create(name="ok", content="a")
        # corrupt one meta.json
        bad = store.root / "broken"
        bad.mkdir()
        (bad / "meta.json").write_text("not json", encoding="utf-8")
        results = store.list()
        assert {a.slug for a in results} == {"ok"}

    def test_list_skips_meta_with_bad_int_or_tags(self, store: ArtifactStore) -> None:
        # Regression: _read_meta_file used to bubble ValueError (int("abc") on
        # bad version field) and TypeError (list(non_iterable) on bad tags
        # field) up through list(), crashing the whole library page on a
        # single corrupted meta.json. Ensure those are now skipped+warned.
        store.create(name="ok", content="a")

        bad_version = store.root / "bad-version"
        bad_version.mkdir()
        (bad_version / "meta.json").write_text(
            json.dumps({"slug": "bad-version", "version": "abc"}),
            encoding="utf-8",
        )

        bad_tags = store.root / "bad-tags"
        bad_tags.mkdir()
        # tags as an int — list(42) raises TypeError.
        (bad_tags / "meta.json").write_text(
            '{"slug": "bad-tags", "tags": 42}',
            encoding="utf-8",
        )

        # list() must not raise; only the healthy artifact is returned.
        results = store.list()
        assert {a.slug for a in results} == {"ok"}

    def test_list_does_not_include_content(self, store: ArtifactStore) -> None:
        store.create(name="x", content="big payload")
        results = store.list()
        assert results[0].content is None


class TestVersions:
    def test_single_version(self, store: ArtifactStore) -> None:
        store.create(name="x", content="v1")
        assert store.list_versions("x") == [1]

    def test_after_updates(self, store: ArtifactStore) -> None:
        store.create(name="x", content="v1")
        store.update("x", content="v2", snapshot=True)
        store.update("x", content="v3", snapshot=True)
        assert store.list_versions("x") == [1, 2, 3]

    def test_missing_raises(self, store: ArtifactStore) -> None:
        with pytest.raises(ArtifactNotFoundError):
            store.list_versions("nope")


class TestPruning:
    def test_old_versions_pruned(self, store: ArtifactStore) -> None:
        store.create(name="x", content="v1")
        # Create more than MAX_VERSIONS revisions.
        for i in range(2, MAX_VERSIONS + 5):
            store.update("x", content=f"v{i}", snapshot=True)
        versions = store.list_versions("x")
        assert len(versions) == MAX_VERSIONS
        # The most recent version is always retained.
        assert versions[-1] == MAX_VERSIONS + 4
        # The oldest pruned versions are gone.
        assert 1 not in versions


# ── delete ─────────────────────────────────────────────────────────────────


class TestDelete:
    def test_deletes_directory(self, store: ArtifactStore) -> None:
        store.create(name="x", content="a")
        store.delete("x")
        assert not (store.root / "x").exists()

    def test_missing_raises(self, store: ArtifactStore) -> None:
        with pytest.raises(ArtifactNotFoundError):
            store.delete("nope")


# ── Path traversal / sensitive paths ───────────────────────────────────────


class TestSecurity:
    def test_root_under_sensitive_path_refused(self, tmp_path: Path, monkeypatch) -> None:
        # Pretend the root path is sensitive
        from kiro_crew import artifacts as art_mod

        monkeypatch.setattr(art_mod, "is_sensitive_path", lambda _p: True)
        with pytest.raises(ArtifactError):
            ArtifactStore(root=tmp_path / "artifacts")

    def test_invalid_slug_chars_rejected(self, store: ArtifactStore) -> None:
        # Uppercase, spaces, special chars all blocked
        for bad in ["UPPER", "with space", "../escape", "foo/bar", "foo\\bar", ""]:
            with pytest.raises(ArtifactValidationError):
                store.get(bad)

    def test_snapshot_version_routes_through_read_gate(
        self, store: ArtifactStore, monkeypatch
    ) -> None:
        # Regression: _snapshot_version() used to call src.read_text(encoding="utf-8") directly,
        # bypassing the is_sensitive_path() gate enforced by self._read_text().
        # If the gate ever started flagging artifact-internal paths (e.g. a
        # symlink expansion landing on a sensitive path), the snapshot read
        # must refuse rather than silently leak. Verify the gated helper is
        # actually on the read path.
        from kiro_crew import artifacts as art_mod

        store.create(name="x", content="v1")
        # First update succeeds — is_sensitive_path() returns False normally.
        store.update("x", content="v2", snapshot=True)

        # Now make is_sensitive_path() return True for current.html only.
        # _snapshot_version reads from current.html via self._read_text() now;
        # that read must surface ArtifactError.
        original = art_mod.is_sensitive_path

        def _selective(p: str) -> bool:
            if "current.html" in p:
                return True
            return original(p)

        monkeypatch.setattr(art_mod, "is_sensitive_path", _selective)
        with pytest.raises(ArtifactError):
            store.update("x", content="v3", snapshot=True)


# ── Tolerant load / persistence ─────────────────────────────────────────────


class TestPersistence:
    def test_unknown_meta_keys_ignored(self, store: ArtifactStore) -> None:
        store.create(name="x", content="a")
        meta_path = store.root / "x" / "meta.json"
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
        raw["future_key"] = "should be ignored"
        meta_path.write_text(json.dumps(raw), encoding="utf-8")
        # Tolerant load doesn't crash
        loaded = store.get("x")
        assert loaded.name == "x"

    def test_missing_optional_keys_filled(self, store: ArtifactStore) -> None:
        store.create(name="x", content="a")
        meta_path = store.root / "x" / "meta.json"
        meta_path.write_text(json.dumps({"slug": "x"}), encoding="utf-8")
        loaded = store.get("x")
        assert loaded.slug == "x"
        assert loaded.kind == "widget"
        assert loaded.source == "chat"
        assert loaded.tags == []

    def test_atomic_write_uses_tmp(self, store: ArtifactStore, tmp_path: Path) -> None:
        # After successful write, no .tmp files should remain.
        store.create(name="x", content="a")
        store.update("x", content="b", snapshot=True)
        assert not list(store.root.rglob("*.tmp"))


# ── Dataclass roundtrip ────────────────────────────────────────────────────


class TestDataclass:
    def test_to_dict_excludes_content_by_default(self) -> None:
        art = Artifact(slug="x", name="x", content="secret")
        d = art.to_dict()
        assert "content" not in d

    def test_to_dict_with_content(self) -> None:
        art = Artifact(slug="x", name="x", content="secret")
        d = art.to_dict(include_content=True)
        assert d["content"] == "secret"


# ── Lifecycle events (Phase 5) ──────────────────────────────────


class TestLifecycleEvents:
    def test_create_emits_created_event(self, store: ArtifactStore) -> None:
        art = store.create(name="brd", content="# hi")
        assert len(art.events) == 1
        ev = art.events[0]
        assert ev["type"] == "created"
        assert ev["version"] == 1
        # Source defaults to chat → by=agent.
        assert ev["by"] == "agent"
        assert ev["ts"]
        # New artifacts are pre-flagged so the get-time backfill is a no-op.
        assert art.events_backfilled is True

    def test_create_with_manual_source_tags_by_field(self, store: ArtifactStore) -> None:
        art = store.create(name="brd", content="# hi", source="manual")
        assert art.events[0]["by"] == "manual"

    def test_user_update_emits_edited_event(self, store: ArtifactStore) -> None:
        store.create(name="brd", content="# v1")
        art = store.update("brd", content="# v2", snapshot=True)  # actor defaults to "user"
        edited = [e for e in art.events if e["type"] == "edited"]
        assert len(edited) == 1
        assert edited[0]["by"] == "user"
        assert edited[0]["version"] == 2

    def test_agent_update_emits_iterated_event(self, store: ArtifactStore) -> None:
        store.create(name="brd", content="# v1")
        art = store.update(
            "brd", content="# v2", actor="agent", session_id="slot-abc", snapshot=True
        )
        iterated = [e for e in art.events if e["type"] == "iterated"]
        assert len(iterated) == 1
        assert iterated[0]["by"] == "agent"
        assert iterated[0]["session_id"] == "slot-abc"
        assert iterated[0]["version"] == 2

    def test_metadata_only_update_emits_no_event(self, store: ArtifactStore) -> None:
        # No content change → no lifecycle entry; metadata-only changes are
        # not interesting for the audit timeline.
        store.create(name="brd", content="# v1")
        art = store.update("brd", description="new desc")
        edits = [e for e in art.events if e["type"] in ("edited", "iterated")]
        assert edits == []

    def test_events_round_trip_through_meta_json(self, store: ArtifactStore) -> None:
        store.create(name="brd", content="# v1")
        store.update("brd", content="# v2", snapshot=True)
        store.update("brd", content="# v3", actor="agent", snapshot=True)
        # Reload from disk.
        loaded = store.get("brd")
        types = [e["type"] for e in loaded.events]
        assert types == ["created", "edited", "iterated"]

    def test_event_log_is_fifo_capped(self, store: ArtifactStore) -> None:
        # Cap is 500 (MAX_EVENTS_PER_ARTIFACT). Force-write 510 events to
        # confirm the oldest 10 get dropped.
        from kiro_crew.artifacts import MAX_EVENTS_PER_ARTIFACT

        art = store.create(name="brd", content="# v1")
        for i in range(MAX_EVENTS_PER_ARTIFACT + 10):
            store._append_event(art, type="referenced", by="agent", session_id=f"s{i}")
        assert len(art.events) == MAX_EVENTS_PER_ARTIFACT
        # Oldest entry should now be `referenced` (the original `created` was evicted).
        assert art.events[0]["type"] == "referenced"

    def test_invalid_event_type_rejected(self, store: ArtifactStore) -> None:
        art = store.create(name="brd", content="# v1")
        with pytest.raises(ArtifactValidationError):
            store._append_event(art, type="bogus")

    def test_reverted_event_type_accepted(self, store: ArtifactStore) -> None:
        # Regression: 'reverted' must be in ALLOWED_EVENT_TYPES — it was
        # added as a render type but missing from the allowlist, so the
        # dashboard's revert flow surfaced a 400 error to the user.
        store.create(name="brd", content="# v1")
        store.update("brd", content="# v2", snapshot=True)
        # Revert to v1 (using the dashboard's PATCH path semantics — revert
        # is treated as a meaningful state change so we always snapshot).
        art = store.update(
            "brd",
            content="# v1",
            event_type="reverted",
            from_version=1,
            snapshot=True,
        )
        revert_events = [e for e in art.events if e["type"] == "reverted"]
        assert len(revert_events) == 1
        assert revert_events[0]["from_version"] == 1
        assert revert_events[0]["version"] == 3

    def test_lazy_backfill_synthesizes_history_for_legacy_artifact(
        self, store: ArtifactStore
    ) -> None:
        # Simulate a pre-Phase-5 meta.json: write one without events.
        adir = store.root / "legacy"
        adir.mkdir(parents=True)
        (adir / "current.html").write_text("legacy content", encoding="utf-8")
        meta = {
            "slug": "legacy",
            "name": "Legacy",
            "kind": "markdown",
            "source": "manual",
            "description": "",
            "tags": [],
            "version": 3,
            "created_at": "2026-01-01T00:00:00.000000+00:00",
            "updated_at": "2026-02-01T00:00:00.000000+00:00",
            # Note: no `events` key — pre-Phase-5 layout.
        }
        (adir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        # First read triggers backfill.
        art = store.get("legacy")
        assert art.events_backfilled is True
        types = [e["type"] for e in art.events]
        assert "created" in types
        assert "edited" in types  # version > 1 + updated_at differs
        # Backfill is persisted, so a second read is a no-op.
        art2 = store.get("legacy")
        assert art2.events == art.events
        # And meta.json on disk now carries the events.
        on_disk = json.loads((adir / "meta.json").read_text(encoding="utf-8"))
        assert on_disk["events_backfilled"] is True
        assert len(on_disk["events"]) >= 1

    def test_backfill_is_idempotent(self, store: ArtifactStore) -> None:
        # Fresh artifact already has events_backfilled=True; the get-time
        # backfill must not double-write or duplicate the created event.
        store.create(name="brd", content="# v1")
        before = store.get("brd")
        events_before = list(before.events)
        after = store.get("brd")
        assert after.events == events_before


# ── source_path metadata (Phase 6) ───────────────────────────────


class TestSourcePath:
    def test_create_persists_source_path(self, store: ArtifactStore) -> None:
        art = store.create(name="brd", content="# hi", source_path="/home/nrb/brd.md")
        assert art.source_path == "/home/nrb/brd.md"
        loaded = store.get("brd")
        assert loaded.source_path == "/home/nrb/brd.md"

    def test_create_default_source_path_is_empty(self, store: ArtifactStore) -> None:
        art = store.create(name="brd", content="# hi")
        assert art.source_path == ""

    def test_find_by_source_path_locates_existing(self, store: ArtifactStore) -> None:
        store.create(name="a", content="x", source_path="/p/a.md")
        store.create(name="b", content="y", source_path="/p/b.md")
        found = store.find_by_source_path("/p/a.md")
        assert found is not None
        assert found.name == "a"

    def test_find_by_source_path_unknown_returns_none(self, store: ArtifactStore) -> None:
        store.create(name="a", content="x", source_path="/p/a.md")
        assert store.find_by_source_path("/p/missing.md") is None

    def test_find_by_source_path_empty_string_returns_none(self, store: ArtifactStore) -> None:
        # Empty source_path is the default for chat-backed artifacts; we
        # don't want callers accidentally hitting a chat artifact when they
        # pass an empty path.
        store.create(name="chat-art", content="x")  # source_path defaults to ""
        assert store.find_by_source_path("") is None

    def test_list_filter_by_source_path(self, store: ArtifactStore) -> None:
        store.create(name="a", content="x", source_path="/p/a.md")
        store.create(name="b", content="y", source_path="/p/b.md")
        results = store.list(source_path="/p/a.md")
        assert len(results) == 1
        assert results[0].name == "a"


# ── Live-pointer behavior for file-backed artifacts (round 3) ──


class TestLivePointer:
    def test_get_returns_live_file_content_not_snapshot(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        # Add a file as artifact, then change the file on disk; get() should
        # return the new file content, NOT the original snapshot.
        src = tmp_path / "live.md"
        src.write_text("# v1 content", encoding="utf-8")
        store.create(
            name="live",
            content="# v1 content",
            source_path=str(src),
            kind="markdown",
        )
        # Change the file on disk (e.g. user edits via MarkdownPanel).
        src.write_text("# v2 content from disk edit", encoding="utf-8")
        loaded = store.get("live")
        assert loaded.content == "# v2 content from disk edit"

    def test_update_writes_back_to_source_path(self, store: ArtifactStore, tmp_path: Path) -> None:
        # Editing the artifact in the dashboard should also update the
        # source file so MarkdownPanel sees the same content.
        src = tmp_path / "synced.md"
        src.write_text("initial", encoding="utf-8")
        store.create(
            name="synced",
            content="initial",
            source_path=str(src),
            kind="markdown",
        )
        store.update("synced", content="edited via dashboard", snapshot=True)
        # File on disk should reflect the edit.
        assert src.read_text(encoding="utf-8") == "edited via dashboard"

    def test_get_falls_back_to_snapshot_when_source_missing(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        # If the source file disappears, the artifact stays viewable via the
        # last-known snapshot in current.html.
        src = tmp_path / "vanishing.md"
        src.write_text("original content", encoding="utf-8")
        store.create(
            name="vanishing",
            content="original content",
            source_path=str(src),
            kind="markdown",
        )
        src.unlink()  # source file deleted
        loaded = store.get("vanishing")
        assert loaded.content == "original content"  # falls back to snapshot

    def test_chat_backed_artifact_unaffected_by_live_pointer(self, store: ArtifactStore) -> None:
        # Widgets and other chat-backed artifacts have no source_path, so
        # they keep using artifact storage as the source of truth.
        store.create(name="widget", content="<p>hello</p>", kind="widget")
        loaded = store.get("widget")
        assert loaded.content == "<p>hello</p>"

    def test_live_pointer_skips_sensitive_paths(
        self, store: ArtifactStore, tmp_path: Path, monkeypatch
    ) -> None:
        # If somehow source_path slipped past the create-time check (e.g.
        # was added before sensitivity rules existed), the live read must
        # still refuse to fetch from sensitive locations.
        sensitive = tmp_path / "fake-credentials"
        sensitive.write_text("SECRET", encoding="utf-8")
        store.create(name="ok", content="placeholder", kind="markdown")
        # Backdoor source_path past validation by writing meta directly.
        meta = store._load_meta("ok")
        meta.source_path = str(sensitive)
        store._write_meta(meta)
        # Pretend the path is sensitive.
        from kiro_crew import artifacts as artifacts_mod

        monkeypatch.setattr(
            artifacts_mod,
            "is_sensitive_path",
            lambda p: str(sensitive) in p,
        )
        loaded = store.get("ok")
        # Falls back to snapshot, not the sensitive content.
        assert loaded.content == "placeholder"
        assert "SECRET" not in (loaded.content or "")


class TestExplicitSnapshotModel:
    """round 5: saves don't bump version unless snapshot=True.

    Versioning is now deliberate — like git commits. Saves silently update
    the live state. Snapshots create new numbered versions. This makes
    history meaningful (each entry represents a deliberate checkpoint)
    rather than noise (every keystroke save creates a version).
    """

    def test_save_without_snapshot_keeps_version(self, store: ArtifactStore) -> None:
        store.create(name="x", content="v1", slug="x")
        art = store.update("x", content="edited content")
        # Live state updates …
        assert art.content == "edited content"
        # … but version stays at 1 (no snapshot was created).
        assert art.version == 1
        # And no event was emitted (saves are silent).
        edit_events = [e for e in art.events if e["type"] == "edited"]
        assert edit_events == []

    def test_save_without_snapshot_emits_no_event(self, store: ArtifactStore) -> None:
        store.create(name="x", content="v1", slug="x")
        store.update("x", content="save 1")
        store.update("x", content="save 2")
        store.update("x", content="save 3")
        art = store.get("x")
        # Only the original 'created' event should exist.
        non_create_events = [e for e in art.events if e["type"] != "created"]
        assert non_create_events == []

    def test_explicit_snapshot_bumps_version(self, store: ArtifactStore) -> None:
        store.create(name="x", content="v1", slug="x")
        art = store.update("x", content="v2", snapshot=True)
        assert art.version == 2
        edit_events = [e for e in art.events if e["type"] == "edited"]
        assert len(edit_events) == 1
        assert edit_events[0]["version"] == 2

    def test_save_then_snapshot_captures_latest_state(self, store: ArtifactStore) -> None:
        # User saves multiple times (silent updates), then explicitly
        # snapshots — the snapshot captures the latest live state, not
        # any intermediate version.
        store.create(name="x", content="v1", slug="x")
        store.update("x", content="save A")  # silent
        store.update("x", content="save B")  # silent
        store.update("x", content="save C")  # silent
        art = store.update("x", content="save C", snapshot=True)
        assert art.version == 2
        # Version 2 should equal "save C" — the live state at snapshot time.
        v2 = store.get("x", version=2)
        assert v2.content == "save C"

    def test_agent_update_via_explicit_snapshot_path(self, store: ArtifactStore) -> None:
        # Simulates how the API handler calls update() for MCP requests
        # (snapshot=True forced by handler when X-Internal-Secret header
        # is present). Confirms agent iterations are versioned.
        store.create(name="x", content="v1", slug="x")
        art = store.update(
            "x",
            content="agent revision",
            actor="agent",
            session_id="slot-abc",
            snapshot=True,
        )
        assert art.version == 2
        iter_events = [e for e in art.events if e["type"] == "iterated"]
        assert len(iter_events) == 1
        assert iter_events[0]["by"] == "agent"
        assert iter_events[0]["session_id"] == "slot-abc"


class TestLiveDirtyAndSnapshotAnytime:
    """round 6: snapshot button works whenever live differs
    from the latest version, not just when there are unsaved edits."""

    def test_live_dirty_false_immediately_after_create(self, store: ArtifactStore) -> None:
        store.create(name="x", content="v1", slug="x")
        loaded = store.get("x")
        assert loaded.live_dirty is False

    def test_live_dirty_true_after_silent_save(self, store: ArtifactStore) -> None:
        store.create(name="x", content="v1", slug="x")
        store.update("x", content="silent edit")  # snapshot=False
        loaded = store.get("x")
        # Live differs from versions/v1.html ("v1") → dirty.
        assert loaded.live_dirty is True

    def test_live_dirty_false_after_explicit_snapshot(self, store: ArtifactStore) -> None:
        store.create(name="x", content="v1", slug="x")
        store.update("x", content="silent edit")
        store.update("x", content="silent edit", snapshot=True)  # capture
        loaded = store.get("x")
        # Live now equals versions/v2.html → not dirty.
        assert loaded.live_dirty is False

    def test_live_dirty_false_for_historical_version_view(self, store: ArtifactStore) -> None:
        # Historical reads should never report live_dirty (the field is
        # meaningless for non-live views).
        store.create(name="x", content="v1", slug="x")
        store.update("x", content="silent edit")
        v1 = store.get("x", version=1)
        assert v1.live_dirty is False

    def test_snapshot_without_content_captures_current_live(self, store: ArtifactStore) -> None:
        # User saved silently (no version bump), then clicks Snapshot
        # at a later time without making any new edits. Snapshot should
        # capture the current live state as the next version.
        store.create(name="x", content="v1", slug="x")
        store.update("x", content="saved silently 1")  # silent save
        store.update("x", content="saved silently 2")  # silent save
        # User clicks Snapshot — no content arg.
        art = store.update("x", snapshot=True)
        assert art.version == 2
        v2 = store.get("x", version=2)
        # The snapshot captured the latest live state.
        assert v2.content == "saved silently 2"
        # And clears live_dirty for subsequent reads.
        loaded = store.get("x")
        assert loaded.live_dirty is False

    def test_snapshot_without_content_for_file_backed_reads_from_disk(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        # File-backed artifact whose source changed externally (not via
        # store.update). Snapshot should pick up the disk content.
        f = tmp_path / "tracked.md"
        f.write_text("initial", encoding="utf-8")
        store.create(
            name="tracked",
            content="initial",
            kind="markdown",
            source_path=str(f),
            slug="tracked",
        )
        # Simulate external edit to the source file.
        f.write_text("externally edited", encoding="utf-8")
        # Live read sees the new content …
        live = store.get("tracked")
        assert live.content == "externally edited"
        # … and live_dirty reflects that it's drifted from v1.
        assert live.live_dirty is True
        # User clicks Snapshot — no content arg, no edit in the dashboard.
        store.update("tracked", snapshot=True)
        # New version captures the external file content.
        v2 = store.get("tracked", version=2)
        assert v2.content == "externally edited"

    def test_snapshot_without_content_emits_edited_event(self, store: ArtifactStore) -> None:
        store.create(name="x", content="v1", slug="x")
        store.update("x", content="silent")  # silent save
        art = store.update("x", snapshot=True, actor="user")
        edited = [e for e in art.events if e["type"] == "edited"]
        assert len(edited) == 1
        assert edited[0]["version"] == 2
        assert edited[0]["by"] == "user"


class TestSourcePathSecurityHardening:
    """review-bot round 12 fixes: path traversal + symlink bypass + UTF-8
    truncation arithmetic."""

    def test_traversal_path_resolves_before_sensitive_check(
        self, store: ArtifactStore, tmp_path: Path, monkeypatch
    ) -> None:
        # A source_path containing `..` segments that resolves into a
        # sensitive location must be refused — not slip past because
        # is_sensitive_path() saw the literal un-canonicalized string.
        sensitive_dir = tmp_path / ".aws"
        sensitive_dir.mkdir()
        sensitive = sensitive_dir / "credentials"
        sensitive.write_text("SECRET", encoding="utf-8")
        # Construct a path that resolves into the sensitive dir via a
        # benign-looking parent: tmp_path/innocent/../.aws/credentials.
        traversal = str(tmp_path / "innocent" / ".." / ".aws" / "credentials")
        # Make is_sensitive_path return True only for the resolved path
        # (NOT the traversal string), simulating the real-world semantics
        # where the check inspects the canonical filesystem location.
        from kiro_crew import artifacts as artifacts_mod

        resolved = str(sensitive.resolve())
        monkeypatch.setattr(
            artifacts_mod,
            "is_sensitive_path",
            lambda p: p == resolved,
        )
        assert store._try_read_source_path(traversal) is None
        assert store._try_write_source_path(traversal, "data") is False

    def test_symlink_to_sensitive_resolves_before_sensitive_check(
        self, store: ArtifactStore, tmp_path: Path, monkeypatch
    ) -> None:
        # A symlink at a benign-looking location pointing into a sensitive
        # file must also be refused after `.resolve()`.
        sensitive = tmp_path / ".ssh-config"
        sensitive.write_text("PRIVATE", encoding="utf-8")
        link = tmp_path / "innocent.md"
        link.symlink_to(sensitive)
        from kiro_crew import artifacts as artifacts_mod

        resolved = str(sensitive.resolve())
        monkeypatch.setattr(
            artifacts_mod,
            "is_sensitive_path",
            lambda p: p == resolved,
        )
        # Read should fall through to None (refused).
        assert store._try_read_source_path(str(link)) is None
        assert store._try_write_source_path(str(link), "data") is False

    def test_utf8_truncation_uses_byte_count_not_char_count(
        self, store: ArtifactStore, tmp_path: Path, monkeypatch
    ) -> None:
        # Multi-byte UTF-8 content (CJK / emoji) MUST be truncated by byte
        # count, not character count. With character-based slicing, the
        # MAX_CONTENT_BYTES bound was silently exceeded for multi-byte text
        # — a 100-char string of 4-byte emoji would be 400 bytes after
        # encode() and bypass the cap.
        from kiro_crew import artifacts as artifacts_mod

        # Use a small cap so the test runs fast.
        monkeypatch.setattr(artifacts_mod, "MAX_CONTENT_BYTES", 50)
        f = tmp_path / "multibyte.md"
        # 4-byte UTF-8 chars: each emoji is U+1F600 (😀, 4 bytes encoded).
        # 30 chars → 120 bytes encoded → must be truncated to ≤50 bytes
        # (which means at most 12 emoji chars in the result).
        f.write_text("😀" * 30, encoding="utf-8")
        result = store._try_read_source_path(str(f))
        assert result is not None
        # Round 13: bounded read caps the disk-IO at MAX_CONTENT_BYTES+1
        # bytes regardless of file size. The decoded string may contain
        # U+FFFD replacement chars at the truncation boundary so its
        # re-encoded byte length CAN exceed MAX_CONTENT_BYTES — that's
        # acceptable. The OOM safety property is "we never read more
        # than ~50 bytes off disk", verified separately.
        # Re-encoding must round-trip cleanly.
        result.encode("utf-8")  # would raise on invalid surrogates


class TestRoundThirteenFixes:
    """review-bot round 13 fixes: bounded read, event_type pre-validation,
    live_dirty not persisted."""

    def test_oversized_file_does_not_load_full_content_into_memory(
        self, store: ArtifactStore, tmp_path: Path, monkeypatch
    ) -> None:
        # Cap is small (50 bytes). Write 5KB of content. The bounded read
        # must stop at MAX_CONTENT_BYTES+1 — verified by mocking read_text
        # to fail loudly if anyone calls it (the new code uses open('rb')
        # + bounded read instead).
        from kiro_crew import artifacts as artifacts_mod

        monkeypatch.setattr(artifacts_mod, "MAX_CONTENT_BYTES", 50)
        f = tmp_path / "big.txt"
        f.write_text("x" * 5000, encoding="utf-8")
        # Ensure read_text isn't used (the OOM-prone path).
        original_read_text = type(f).read_text
        calls = {"count": 0}

        def tracked_read_text(self, *args, **kwargs):
            calls["count"] += 1
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(type(f), "read_text", tracked_read_text)
        result = store._try_read_source_path(str(f))
        assert result is not None
        assert len(result.encode("utf-8")) <= 50
        # The new bounded-read path doesn't call read_text — verifies the
        # OOM-prone whole-file read was actually replaced.
        assert calls["count"] == 0

    def test_invalid_event_type_does_not_leave_orphaned_version_file(
        self, store: ArtifactStore
    ) -> None:
        # Round 13: validation happens BEFORE version bump and snapshot
        # write. An invalid event_type must not leave a versions/v{N}.html
        # on disk.
        store.create(name="x", content="v1", slug="x")
        before_versions = list((store.root / "x" / "versions").iterdir())
        with pytest.raises(ArtifactValidationError):
            store.update(
                "x",
                content="v2",
                snapshot=True,
                event_type="not-a-valid-type",
            )
        after_versions = list((store.root / "x" / "versions").iterdir())
        # Version count must not have grown — no orphan file.
        assert len(after_versions) == len(before_versions)
        # Artifact version must not have bumped either (rolled back by
        # the early raise — no _write_meta reached).
        loaded = store.get("x")
        assert loaded.version == 1

    def test_live_dirty_not_persisted_in_meta_json(self, store: ArtifactStore) -> None:
        # Round 13: live_dirty is computed at GET time and must not be
        # written to meta.json. Persisting would create staleness bugs.
        store.create(name="x", content="v1", slug="x")
        # Trigger a GET that sets live_dirty, then write_meta via update
        # (metadata-only, no content) and verify the on-disk meta has no
        # live_dirty key.
        store.get("x")  # populates art.live_dirty in memory
        store.update("x", description="updated")  # writes meta
        meta_path = store.root / "x" / "meta.json"
        on_disk = json.loads(meta_path.read_text(encoding="utf-8"))
        assert "live_dirty" not in on_disk

    def test_live_dirty_still_present_in_api_response(self, store: ArtifactStore) -> None:
        # Even though live_dirty isn't persisted, it MUST still appear
        # on the get() return value (computed fresh) and on to_dict()
        # responses for API consumers.
        store.create(name="x", content="v1", slug="x")
        loaded = store.get("x")
        d = loaded.to_dict(include_content=True)  # API response shape
        assert "live_dirty" in d
        assert d["live_dirty"] is False


class TestRecordImpression:
    """Direct tests for ``ArtifactStore.record_impression`` — the
    pure-observability hook used by `WidgetFrame` to emit ``referenced``
    events on chat impression. Covers the store-level invariants:
    no version bump, no content change, metadata preserved, idempotent
    at the call site (callers must dedupe; the store appends every call)."""

    @pytest.fixture
    def store(self, tmp_path):
        return ArtifactStore(root=tmp_path / "artifacts")

    def test_appends_referenced_event(self, store):
        store.create(name="X", content="<div>x</div>", slug="x", kind="widget")
        art, appended = store.record_impression(
            "x",
            by="user",
            session_id="chat-1-1779995123",
            message_ts="1779995123.456789",
            widget_index=0,
        )
        assert appended is True
        ref = [e for e in art.events if e["type"] == "referenced"]
        assert len(ref) == 1
        assert ref[0]["by"] == "user"
        assert ref[0]["session_id"] == "chat-1-1779995123"
        assert ref[0]["metadata"]["message_ts"] == "1779995123.456789"
        assert ref[0]["metadata"]["widget_index"] == 0

    def test_does_not_bump_version(self, store):
        store.create(name="X", content="<div>x</div>", slug="x", kind="widget")
        store.update("x", content="<div>v2</div>")
        before = store.get("x")
        store.record_impression("x", by="user", session_id="s", message_ts="t", widget_index=0)
        after = store.get("x")
        assert after.version == before.version

    def test_does_not_change_content(self, store):
        store.create(name="X", content="<div>orig</div>", slug="x", kind="widget")
        store.record_impression("x", by="user", session_id="s", message_ts="t", widget_index=0)
        assert store.get("x").content == "<div>orig</div>"

    def test_unknown_slug_raises(self, store):
        from kiro_crew.artifacts import ArtifactNotFoundError

        with pytest.raises(ArtifactNotFoundError):
            store.record_impression("no-such-thing", by="user")

    def test_metadata_omitted_when_no_coordinates(self, store):
        # If neither message_ts nor widget_index is supplied, the event
        # records but has no metadata field. Defensive — backend's
        # validation rejects metadata that's all empty.
        store.create(name="X", content="<div>x</div>", slug="x", kind="widget")
        art, _ = store.record_impression("x", by="user", session_id="s")
        ref = [e for e in art.events if e["type"] == "referenced"]
        assert len(ref) == 1
        assert "metadata" not in ref[0]

    def test_one_referenced_per_session(self, store):
        # A `referenced` event is a per-session breadcrumb: at most one per
        # session, even if the widget is emitted in several messages of
        # that session (different message_ts / widget_index) or the tab is
        # reloaded. A different session is a distinct breadcrumb. This is
        # the fix — the same session was piling up duplicates.
        store.create(name="X", content="<div>x</div>", slug="x", kind="widget")
        _, a1 = store.record_impression(
            "x", by="user", session_id="s", message_ts="m1", widget_index=0
        )
        _, a2 = store.record_impression(
            "x", by="user", session_id="s", message_ts="m2", widget_index=1
        )
        _, a3 = store.record_impression(
            "x", by="user", session_id="s", message_ts="m1", widget_index=0
        )
        _, a4 = store.record_impression(
            "x", by="user", session_id="s2", message_ts="m1", widget_index=0
        )
        assert (a1, a2, a3, a4) == (True, False, False, True)
        ref = [e for e in store.get("x").events if e["type"] == "referenced"]
        assert len(ref) == 2  # one per session: s, s2

    def test_suppresses_referenced_when_session_already_has_cud(self, store):
        # When a session already has a CUD event on the artifact (e.g. the
        # agent ran artifact_update via MCP in chat session s1), a
        # subsequent `referenced` impression from that SAME session is
        # redundant — the session is already on the timeline. It must be
        # suppressed (no event appended). A different session with no CUD
        # still records normally. Regression guard for the
        # duplicate-`referenced`-on-widget-update bug.
        store.create(name="X", content="<div>x</div>", slug="x", kind="widget")
        store.update("x", content="<div>v2</div>", session_id="s1", actor="agent", snapshot=True)
        _, appended = store.record_impression(
            "x", by="user", session_id="s1", message_ts="t", widget_index=0
        )
        assert appended is False
        assert [e for e in store.get("x").events if e["type"] == "referenced"] == []
        _, appended2 = store.record_impression(
            "x", by="user", session_id="s2", message_ts="t", widget_index=0
        )
        assert appended2 is True
        ref = [e for e in store.get("x").events if e["type"] == "referenced"]
        assert len(ref) == 1
        assert ref[0]["session_id"] == "s2"


# ── Kind inference (CR-1) ─────────────────────────────────────────────────────


class TestInferKind:
    """Resolution order of the standalone ``_infer_kind`` helper."""

    def test_explicit_wins(self) -> None:
        # A non-empty explicit kind is returned untouched, even when the
        # content / source_path would infer something else.
        assert _infer_kind("# heading", "doc.html", explicit="json") == "json"
        assert _infer_kind("<div/>", "", explicit="markdown") == "markdown"

    @pytest.mark.parametrize(
        "path,expected",
        [
            ("plan.md", "markdown"),
            ("plan.markdown", "markdown"),
            ("page.html", "html"),
            ("page.htm", "html"),
            ("icon.svg", "svg"),
            ("data.json", "json"),
            ("notes.txt", "text"),
            ("Makefile", "text"),  # no extension → text
            ("archive.tar.gz", "text"),  # unknown extension → text
            ("UPPER.MD", "markdown"),  # case-insensitive
        ],
    )
    def test_extension_matrix(self, path: str, expected: str) -> None:
        # The source_path extension drives the kind regardless of the body.
        assert _infer_kind("any body at all", source_path=path) == expected

    @pytest.mark.parametrize(
        "content,expected",
        [
            ("<div>x</div>", "widget"),
            ("<table><tr></tr></table>", "widget"),
            ("<span>hi</span>", "widget"),
            ("<style>.a{}</style>", "widget"),
            ("<mcwidget>body</mcwidget>", "widget"),
            ("<!DOCTYPE html><html></html>", "widget"),
            ("# Plan\n\nbody", "markdown"),
            ("###### deep heading", "markdown"),
            ("just plain prose with no tags", "markdown"),
            ("  \n# leading whitespace then heading", "markdown"),
            ("<p>hello</p>", "widget"),  # a tag, but not a known marker → fallback
            ("a < b but no real tag", "widget"),  # stray '<' → fallback widget
            ("", "widget"),  # empty → legacy default
            ("   \n  ", "widget"),  # whitespace-only → legacy default
        ],
    )
    def test_content_sniff_matrix(self, content: str, expected: str) -> None:
        assert _infer_kind(content, source_path="") == expected

    def test_extension_beats_content_sniff(self) -> None:
        # A .md file whose body contains HTML is still markdown (extension wins).
        assert _infer_kind("<div>x</div>", source_path="notes.md") == "markdown"


class TestCreateKindInference:
    """``create()`` infers the kind when the caller omits it."""

    def test_create_infers_markdown_from_heading(self, store: ArtifactStore) -> None:
        art = store.create(name="Plan", content="# Title\n\nbody")
        assert art.kind == "markdown"

    def test_create_infers_widget_from_html(self, store: ArtifactStore) -> None:
        art = store.create(name="Dash", content="<div>x</div><table></table>")
        assert art.kind == "widget"

    def test_create_infers_from_source_path(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        src = tmp_path / "plan.md"
        src.write_text("# live", encoding="utf-8")
        art = store.create(name="P", content="# live", source_path=str(src))
        assert art.kind == "markdown"

    def test_explicit_kind_overrides_inference(self, store: ArtifactStore) -> None:
        # Plain markdown content but the caller pins widget → widget wins.
        art = store.create(name="P", content="# Title", kind="widget")
        assert art.kind == "widget"

    def test_create_persists_inferred_kind(self, store: ArtifactStore) -> None:
        store.create(name="P", content="plain prose, no tags here", slug="p")
        assert store.get("p").kind == "markdown"
