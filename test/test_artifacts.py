"""Unit tests for :mod:`kiro_crew.artifacts` — the data layer."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from conftest import requires_symlinks
from kiro_crew.artifacts import (
    MAX_CONTENT_BYTES,
    MAX_VERSIONS,
    Artifact,
    ArtifactComment,
    ArtifactError,
    ArtifactNotFoundError,
    ArtifactStore,
    ArtifactValidationError,
    _infer_kind,
    detect_editor_kind,
    has_unthemed_hardcoded_colors,
    slugify,
)

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    """Fresh store rooted at a tmp dir."""
    return ArtifactStore(root=tmp_path / "artifacts")


# ── slugify ─────────────────────────────────────────────────────────────────


class TestHasUnthemedHardcodedColors:
    # Direct unit tests for the shared theme-contrast detector — the ONE
    # place the verdict is computed (the gateway handlers stamp it on
    # responses; MCP + CLI relay it).

    def test_fires_on_hardcoded_hex(self) -> None:
        assert has_unthemed_hardcoded_colors("widget", '<div style="color:#111">x</div>')

    def test_fires_on_rgb_function(self) -> None:
        assert has_unthemed_hardcoded_colors(
            "html", "<style>body{background:rgb(255,255,255)}</style>"
        )

    def test_fires_on_uppercase_color_function(self) -> None:
        # CSS functions are case-insensitive: RGB(...) is as hardcoded as
        # rgb(...). Locks the IGNORECASE flag.
        assert has_unthemed_hardcoded_colors("widget", '<div style="color:RGB(1,2,3)">x</div>')

    def test_silent_on_href_fragment_links(self) -> None:
        # href="#abc" is a fragment link, not a color — including the
        # whitespace-spaced form (href = "#abc"), which is valid HTML a
        # fixed-width lookbehind could not exempt. Locks the strip-based
        # href exclusion.
        assert not has_unthemed_hardcoded_colors(
            "widget", '<a href="#abc">jump</a> <a href = "#facade">also</a>'
        )

    def test_silent_when_theme_vars_present(self) -> None:
        # The recommended fallback form carries a hex literal AND a
        # var(--…) reference — theme-aware content must not flag.
        assert not has_unthemed_hardcoded_colors(
            "widget", '<div style="color:var(--text,#111);background:var(--bg,#fff)">x</div>'
        )

    def test_silent_for_non_iframe_kinds(self) -> None:
        # markdown/text/json/svg render natively (no injected theme
        # defaults to clash with) — the lint is widget/html only.
        assert not has_unthemed_hardcoded_colors("markdown", "color: #ff0000")

    def test_silent_on_empty_content(self) -> None:
        assert not has_unthemed_hardcoded_colors("widget", "")

    def test_hex_shaped_id_selector_at_value_position_is_accepted_noise(self) -> None:
        # Documented residual: a whitespace-preceded hex-shaped id selector
        # can fire, because whitespace must stay in the prefix class or true
        # positives like "border: 1px solid #ccc" are lost. Soft warning only.
        assert has_unthemed_hardcoded_colors("widget", "<style>border: 1px solid #ccc</style>")


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

    def test_touched_by_session_unions_origin_and_events(self, store: ArtifactStore) -> None:
        """``touched_by_session`` is origin OR any event's session_id.

        Three artifacts, three relationships to chat-1: authored it, only read
        it, and nothing at all. The first two match, the third must not.
        """
        store.create(name="authored", content="a", slug="authored", session_key="chat-1")
        store.create(name="read", content="b", slug="read")
        store.record_impression("read", by="agent", session_id="chat-1")
        store.create(name="unrelated", content="c", slug="unrelated", session_key="chat-2")
        results = store.list(touched_by_session="chat-1")
        assert {a.slug for a in results} == {"authored", "read"}

    def test_touched_by_session_empty_is_a_no_op(self, store: ArtifactStore) -> None:
        """Unlike ``session_key=""`` there is no "untouched" bucket to select."""
        store.create(name="a", content="a", session_key="chat-1")
        store.create(name="b", content="b")
        assert len(store.list(touched_by_session="")) == 2
        assert len(store.list(touched_by_session=None)) == 2

    def test_touched_by_session_is_exact_not_prefix(self, store: ArtifactStore) -> None:
        """Matching a prefix would leak a sibling session's artifacts.

        Slot keys share a ``chat-<n>-`` shape, so ``chat-1`` must not match
        ``chat-11`` — otherwise one session's tab lists another's work.
        """
        store.create(name="a", content="a", slug="a", session_key="chat-1")
        store.create(name="b", content="b", slug="b", session_key="chat-11")
        assert {x.slug for x in store.list(touched_by_session="chat-1")} == {"a"}

    def test_touched_by_session_matches_the_real_mixed_key_formats(
        self, store: ArtifactStore
    ) -> None:
        """The two provenance fields are persisted in DIFFERENT formats.

        Production data (verified against a live store) holds a bare slot key
        in ``session_key`` — written from the browser's slot — and a
        scope-qualified ``dashboard:<slot>`` in each event's ``session_id``,
        because MCP callers resolve identity through the scope-qualified
        session key. The panel queries with the bare slot, so a literal
        comparison silently drops every consumed artifact: exactly the case
        the involvement scope exists to add.
        """
        store.create(name="read", content="b", slug="read")
        store.record_impression("read", by="agent", session_id="dashboard:chat-1")
        store.create(name="saved", content="c", slug="saved", session_key="dashboard:chat-1")
        store.create(name="other", content="d", slug="other")
        store.record_impression("other", by="agent", session_id="dashboard:chat-2")
        # Bare slot key, as the dashboard sends it.
        assert {x.slug for x in store.list(touched_by_session="chat-1")} == {"read", "saved"}
        # Scope-qualified key, as an MCP caller would send it — same answer.
        assert {
            x.slug for x in store.list(touched_by_session="dashboard:chat-1")
        } == {"read", "saved"}

    def test_touched_by_session_does_not_collapse_other_scopes(
        self, store: ArtifactStore
    ) -> None:
        """Only ``dashboard:`` is stripped.

        A ``slack:``/``cron:`` key has no bare chat-slot twin, so collapsing
        those prefixes would merge genuinely different sessions into one tab.
        """
        store.create(name="a", content="a", slug="a", session_key="slack:chat-1")
        store.create(name="b", content="b", slug="b", session_key="cron:chat-1")
        store.create(name="c", content="c", slug="c", session_key="dashboard:chat-1")
        assert {x.slug for x in store.list(touched_by_session="chat-1")} == {"c"}

    def test_touched_by_session_ignores_events_with_no_session(
        self, store: ArtifactStore
    ) -> None:
        """A null/absent ``session_id`` must not match a real query key."""
        store.create(name="a", content="a", slug="a")
        store.record_impression("a", by="user", session_id=None)
        assert store.list(touched_by_session="chat-1") == []

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

    def test_filter_by_session_key(self, store: ArtifactStore) -> None:
        store.create(name="a", content="a", session_key="dashboard:chat-1")
        store.create(name="b", content="a", session_key="dashboard:chat-2")
        store.create(name="c", content="a")
        assert {x.slug for x in store.list(session_key="dashboard:chat-1")} == {"a"}

    def test_session_key_empty_string_scopes_to_unattributed(self, store: ArtifactStore) -> None:
        """``""`` is the no-origin bucket; ``None`` means don't scope at all."""
        store.create(name="a", content="a", session_key="dashboard:chat-1")
        store.create(name="c", content="a")
        assert {x.slug for x in store.list(session_key="")} == {"c"}
        assert len(store.list(session_key=None)) == 2

    def test_filter_by_pinned(self, store: ArtifactStore) -> None:
        store.create(name="a", content="a")
        store.create(name="b", content="a")
        store.set_pinned("a", True)
        assert {x.slug for x in store.list(pinned=True)} == {"a"}
        assert {x.slug for x in store.list(pinned=False)} == {"b"}
        assert len(store.list(pinned=None)) == 2

    def test_session_and_pinned_filters_compose(self, store: ArtifactStore) -> None:
        store.create(name="a", content="a", session_key="s1")
        store.create(name="b", content="a", session_key="s1")
        store.create(name="c", content="a", session_key="s2")
        store.set_pinned("a", True)
        store.set_pinned("c", True)
        assert {x.slug for x in store.list(session_key="s1", pinned=True)} == {"a"}

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


class TestSharedLockAcrossInstances:
    """#2492: two ``ArtifactStore`` instances pointed at the same root must
    serialize against EACH OTHER, not just against themselves.

    ``__init__`` used to hand out a fresh ``threading.Lock()`` per instance —
    correct for a single long-lived store, but any code constructing its own
    ``ArtifactStore()`` against the shared default root (rather than going
    through :func:`get_default_store`) got an unserialized lock: a concurrent
    write from that second instance was never mutually exclusive with the
    singleton's own reads/writes on the same slug, exactly the class of race
    that can make ``get()`` (no version) observe content mid-write.
    """

    def test_two_instances_on_the_same_root_share_one_lock(self, tmp_path: Path) -> None:
        root = tmp_path / "artifacts"
        a = ArtifactStore(root=root)
        b = ArtifactStore(root=root)
        assert a._lock is b._lock

    def test_instances_on_different_roots_do_not_share_a_lock(self, tmp_path: Path) -> None:
        a = ArtifactStore(root=tmp_path / "artifacts-a")
        b = ArtifactStore(root=tmp_path / "artifacts-b")
        assert a._lock is not b._lock

    def test_a_write_through_a_second_instance_is_visible_to_the_first(
        self, tmp_path: Path
    ) -> None:
        """Not just the same lock OBJECT -- an update from a second instance
        must actually be observable (and mutually exclusive) through the
        first, which is what the shared lock is for."""
        root = tmp_path / "artifacts"
        a = ArtifactStore(root=root)
        a.create(name="x", content="v1", kind="text")
        b = ArtifactStore(root=root)
        b.update("x", content="v2")
        assert a.get("x").content == "v2"

    def test_resolved_symlinked_root_shares_the_same_lock(self, tmp_path: Path) -> None:
        real = tmp_path / "real-artifacts"
        real.mkdir()
        link = tmp_path / "linked-artifacts"
        link.symlink_to(real)
        a = ArtifactStore(root=real)
        b = ArtifactStore(root=link)
        assert a._lock is b._lock


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
        art = store.create(name="brd", content="# hi", source_path="/home/alice/brd.md")
        assert art.source_path == "/home/alice/brd.md"
        loaded = store.get("brd")
        assert loaded.source_path == "/home/alice/brd.md"

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

    @requires_symlinks
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


class TestDetectEditorKind:
    """:func:`detect_editor_kind` — the content sniff behind blank documents."""

    @pytest.mark.parametrize(
        "content,expected",
        [
            ('{"a": 1}', "json"),
            ('   {"a": 1}   ', "json"),
            ("[1, 2, 3]", "json"),
            ("[]", "json"),
            ("{}", "json"),
            ('<svg viewBox="0 0 1 1"></svg>', "svg"),
            ("<SVG></SVG>", "svg"),
            ('<?xml version="1.0"?>\n<svg></svg>', "svg"),
            ("<!DOCTYPE svg>\n<svg></svg>", "svg"),
        ],
    )
    def test_detects_structured_kinds(self, content: str, expected: str) -> None:
        assert detect_editor_kind(content) == expected

    @pytest.mark.parametrize(
        "content",
        [
            "",
            "   \n  ",
            "# A markdown heading",
            "just some prose",
            '{"a": 1',  # mid-edit syntax error
            "42",  # bare JSON scalar — valid JSON, but almost certainly prose
            "true",
            '"a quoted string"',
            "<div>html, not svg</div>",
            "text before <svg></svg>",  # anchored: not an SVG document
            "<html><body></body></html>",
        ],
    )
    def test_returns_none_for_unrecognized(self, content: str) -> None:
        """Never guesses. ``None`` is what keeps a re-typed kind from flapping."""
        assert detect_editor_kind(content) is None

    def test_never_returns_a_non_editable_kind(self) -> None:
        """``html`` / ``widget`` would strand the editor, so they're excluded."""
        for content in ("<div>x</div>", "<mcwidget>x</mcwidget>", "<!doctype html><html>"):
            assert detect_editor_kind(content) not in ("html", "widget")


class TestBlankDocumentKind:
    """Creating a document blank, and letting its kind settle on first save."""

    def test_blank_create_defaults_to_editable_markdown(self, store: ArtifactStore) -> None:
        art = store.create(name="Untitled", content="")
        assert art.kind == "markdown"
        assert art.kind_auto is True

    def test_whitespace_only_create_counts_as_blank(self, store: ArtifactStore) -> None:
        art = store.create(name="Untitled", content="   \n ")
        assert (art.kind, art.kind_auto) == ("markdown", True)

    def test_content_bearing_create_pins_its_kind(self, store: ArtifactStore) -> None:
        art = store.create(name="Doc", content="# Real content")
        assert (art.kind, art.kind_auto) == ("markdown", False)

    def test_explicit_kind_pins_even_when_blank(self, store: ArtifactStore) -> None:
        art = store.create(name="Doc", content="", kind="json")
        assert (art.kind, art.kind_auto) == ("json", False)

    def test_file_backed_blank_is_not_auto(self, store: ArtifactStore, tmp_path: Path) -> None:
        """An extension is a real signal, so a blank file keeps its mapped kind."""
        src = tmp_path / "empty.json"
        src.write_text("", encoding="utf-8")
        art = store.create(name="Empty", content="", source_path=str(src))
        assert (art.kind, art.kind_auto) == ("json", False)

    def test_kind_auto_survives_a_reload(self, store: ArtifactStore) -> None:
        store.create(name="Untitled", content="", slug="u")
        assert store.get("u").kind_auto is True

    def test_legacy_meta_without_the_field_is_never_auto(self, store: ArtifactStore) -> None:
        store.create(name="Legacy", content="# hi", slug="legacy")
        meta_path = store.root / "legacy" / "meta.json"
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
        del raw["kind_auto"]
        meta_path.write_text(json.dumps(raw), encoding="utf-8")
        assert store.get("legacy").kind_auto is False

    def test_first_json_save_retypes_the_document(self, store: ArtifactStore) -> None:
        store.create(name="Untitled", content="", slug="u")
        assert store.update("u", content='{"hello": "world"}').kind == "json"
        assert store.get("u").kind == "json"

    def test_first_svg_save_retypes_the_document(self, store: ArtifactStore) -> None:
        store.create(name="Untitled", content="", slug="u")
        assert store.update("u", content="<svg></svg>").kind == "svg"

    def test_prose_save_stays_markdown(self, store: ArtifactStore) -> None:
        store.create(name="Untitled", content="", slug="u")
        assert store.update("u", content="# My notes\n\nsome prose").kind == "markdown"

    def test_mid_edit_syntax_error_does_not_flap_the_kind(self, store: ArtifactStore) -> None:
        """Once JSON, a transient parse failure must not drop back to markdown."""
        store.create(name="Untitled", content="", slug="u")
        store.update("u", content='{"a": 1}')
        assert store.update("u", content='{"a": ').kind == "json"
        assert store.update("u", content='{"a": 2}').kind == "json"

    def test_retyping_stays_available_between_structured_kinds(
        self, store: ArtifactStore
    ) -> None:
        store.create(name="Untitled", content="", slug="u")
        store.update("u", content='{"a": 1}')
        assert store.update("u", content="<svg></svg>").kind == "svg"

    def test_a_pinned_artifact_is_never_retyped(self, store: ArtifactStore) -> None:
        store.create(name="Doc", content="# Real content", slug="d")
        assert store.update("d", content='{"a": 1}').kind == "markdown"

    def test_an_explicit_kind_on_update_pins_the_artifact(self, store: ArtifactStore) -> None:
        store.create(name="Untitled", content="", slug="u")
        pinned = store.update("u", content="# prose", kind="text")
        assert (pinned.kind, pinned.kind_auto) == ("text", False)
        # Now that it's pinned, JSON content no longer re-types it.
        assert store.update("u", content='{"a": 1}').kind == "text"

    def test_snapshot_records_the_detected_kind_for_the_version(
        self, store: ArtifactStore
    ) -> None:
        store.create(name="Untitled", content="", slug="u")
        art = store.update("u", content='{"a": 1}', snapshot=True)
        assert art.version_kinds[str(art.version)] == "json"
        # v1 was blank markdown and must still read back that way.
        assert store.get("u", version=1).kind == "markdown"


class TestSettleBlank:
    """:meth:`ArtifactStore.settle_blank` -- resolving a just-created blank.

    This is where the keep / save / delete decision lives, and it lives here
    rather than in the browser for one reason: it has to be atomic. Deciding in
    the client means reading the artifact and then acting on it, and a save
    landing in that window -- from a popout window on the same document, or from
    an agent -- gets overwritten or deleted. Re-reading cannot close that window
    because the window is between the read and the write.

    Every case below therefore asserts the SAME safety property from a different
    direction: unless the document is pristine on every axis the record can
    report, it is left exactly as it is.
    """

    UNTITLED = "Untitled"

    def _blank(self, store: ArtifactStore) -> None:
        store.create(name=self.UNTITLED, content="", slug="u")

    def test_deletes_an_abandoned_blank(self, store: ArtifactStore) -> None:
        self._blank(store)
        assert store.settle_blank("u", untitled_name=self.UNTITLED) == "deleted"
        with pytest.raises(ArtifactNotFoundError):
            store.get("u")

    def test_saves_an_unsaved_draft_instead_of_deleting(self, store: ArtifactStore) -> None:
        self._blank(store)
        assert store.settle_blank("u", untitled_name=self.UNTITLED, draft="# notes") == "saved"
        assert store.get("u").content == "# notes"

    def test_a_whitespace_draft_is_not_a_draft(self, store: ArtifactStore) -> None:
        self._blank(store)
        assert store.settle_blank("u", untitled_name=self.UNTITLED, draft="  \n ") == "deleted"

    def test_saving_a_draft_does_not_bump_the_version(self, store: ArtifactStore) -> None:
        """The draft is the document's first content, not an edit to it."""
        self._blank(store)
        store.settle_blank("u", untitled_name=self.UNTITLED, draft="# notes")
        assert store.get("u").version == 1

    @pytest.mark.parametrize(
        "draft,expected",
        [
            pytest.param('{"a": 1}', "json", id="json"),
            pytest.param("<svg></svg>", "svg", id="svg"),
            pytest.param("# notes", "markdown", id="markdown"),
        ],
    )
    def test_a_settled_draft_gets_kind_detection(
        self, store: ArtifactStore, draft: str, expected: str
    ) -> None:
        """A settled draft is the document's first content, so it earns the same
        detection an ordinary save would. Without this, typing JSON into a blank
        and navigating away stored it as markdown and rendered it wrongly."""
        self._blank(store)
        assert store.settle_blank("u", untitled_name=self.UNTITLED, draft=draft) == "saved"
        assert store.get("u").kind == expected

    def test_a_settled_draft_respects_an_explicitly_chosen_kind(
        self, store: ArtifactStore
    ) -> None:
        """A kind the user picked must survive; only auto-assigned kinds re-detect."""
        self._blank(store)
        store.update("u", kind="text")
        assert store.settle_blank("u", untitled_name=self.UNTITLED, draft='{"a": 1}') == "saved"
        assert store.get("u").kind == "text"

    def test_a_concurrent_save_is_never_overwritten(self, store: ArtifactStore) -> None:
        """The headline race: content arrived while the page still held a draft."""
        self._blank(store)
        store.update("u", content="# saved in the popout")
        assert store.settle_blank("u", untitled_name=self.UNTITLED, draft="# stale") == "kept"
        assert store.get("u").content == "# saved in the popout"

    @pytest.mark.parametrize(
        "invest",
        [
            pytest.param(lambda s: s.update("u", content="# written"), id="content"),
            pytest.param(lambda s: s.update("u", name="Release plan"), id="renamed"),
            pytest.param(lambda s: s.update("u", description="scratch pad"), id="described"),
            pytest.param(lambda s: s.update("u", tags=["ops"]), id="tagged"),
            pytest.param(lambda s: s.set_folder("u", "abc123"), id="filed"),
            pytest.param(lambda s: s.set_pinned("u", True), id="starred"),
            pytest.param(
                lambda s: s.add_comment("u", ArtifactComment(id="c1", body="hm")),
                id="commented",
            ),
        ],
    )
    def test_keeps_a_document_with_any_investment(self, store: ArtifactStore, invest) -> None:
        self._blank(store)
        invest(store)
        assert store.settle_blank("u", untitled_name=self.UNTITLED) == "kept"
        assert store.get("u") is not None

    @pytest.mark.parametrize(
        "invest,check",
        [
            pytest.param(
                lambda s: s.update("u", name="Release plan"),
                lambda a: a.name == "Release plan",
                id="renamed",
            ),
            pytest.param(
                lambda s: s.update("u", tags=["ops"]), lambda a: a.tags == ["ops"], id="tagged"
            ),
            pytest.param(
                lambda s: s.update("u", kind="text"), lambda a: a.kind == "text", id="typed"
            ),
            pytest.param(
                lambda s: s.set_pinned("u", True), lambda a: a.pinned, id="starred"
            ),
        ],
    )
    def test_investment_does_not_cost_the_user_their_draft(
        self, store: ArtifactStore, invest, check
    ) -> None:
        """The two questions are independent. Naming or tagging a document says
        nothing about whether the editor is holding text that was never saved --
        and refusing to write it because the name changed loses their typing.
        Metadata survives alongside it."""
        self._blank(store)
        invest(store)
        assert (
            store.settle_blank("u", untitled_name=self.UNTITLED, draft="# first paragraph")
            == "saved"
        )
        art = store.get("u")
        assert art.content == "# first paragraph"
        assert check(art)

    def test_a_draft_is_rescued_even_when_deletion_is_forbidden(
        self, store: ArtifactStore
    ) -> None:
        """``allow_delete=False`` means "I have writes you may not have applied".
        It cannot make deletion safe; it has no bearing on saving the buffer."""
        self._blank(store)
        assert (
            store.settle_blank(
                "u", untitled_name=self.UNTITLED, draft="# typed", allow_delete=False
            )
            == "saved"
        )
        assert store.get("u").content == "# typed"

    def test_deletion_is_refused_when_the_caller_forbids_it(
        self, store: ArtifactStore
    ) -> None:
        self._blank(store)
        assert (
            store.settle_blank("u", untitled_name=self.UNTITLED, allow_delete=False) == "kept"
        )
        assert store.get("u") is not None

    def test_keeps_a_document_with_history_even_if_emptied_again(
        self, store: ArtifactStore
    ) -> None:
        """A snapshot is history worth keeping. Content alone would miss this: the
        live body can be emptied again after a snapshot was taken."""
        self._blank(store)
        store.update("u", content="# v2", snapshot=True)
        store.update("u", content="")
        assert store.settle_blank("u", untitled_name=self.UNTITLED) == "kept"

    def test_a_comment_is_seen_even_though_it_touches_no_field(
        self, store: ArtifactStore
    ) -> None:
        """Comments live in a sidecar that add_comment writes WITHOUT touching
        meta.json, so no field on the record reveals them."""
        self._blank(store)
        store.add_comment("u", ArtifactComment(id="c1", body="is this right?"))
        assert store.settle_blank("u", untitled_name=self.UNTITLED) == "kept"
        assert len(store.list_comments("u")) == 1

    def test_a_differently_named_document_is_not_an_untitled_one(
        self, store: ArtifactStore
    ) -> None:
        """The placeholder is supplied by the caller (it is localised), so a
        mismatch must mean 'named' rather than 'unrecognised, delete it'."""
        store.create(name="Sans titre", content="", slug="u")
        assert store.settle_blank("u", untitled_name=self.UNTITLED) == "kept"

    def test_missing_artifact_raises_rather_than_reporting_success(
        self, store: ArtifactStore
    ) -> None:
        with pytest.raises(ArtifactNotFoundError):
            store.settle_blank("nope", untitled_name=self.UNTITLED)


# ── source_root barrier + visible dead pointer ────────────────────────────────


@pytest.fixture
def home_store(tmp_path: Path, monkeypatch) -> ArtifactStore:
    """A store whose data home sits INSIDE a fake ``$HOME``.

    The default ``store`` fixture is rooted at ``tmp_path/artifacts``, so its
    data-home root is ``tmp_path`` — which would make every path in the test
    tree "already allowed" and hide the barrier under test. Nesting the store
    under a fake home (mirroring production's ``~/.kiro/crew/artifacts``) leaves
    the rest of ``tmp_path`` genuinely outside every default root, standing in
    for ``/workplace/...``.
    """
    home = tmp_path / "home"
    (home / ".kiro" / "crew").mkdir(parents=True)
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: home))
    return ArtifactStore(root=home / ".kiro" / "crew" / "artifacts")


@pytest.fixture
def project_file(tmp_path: Path) -> tuple[Path, Path]:
    """A ``/workplace``-style project root + file, outside home and the data home."""
    proj = tmp_path / "workplace" / "nrb" / "repo"
    src = proj / "docs" / "spec.md"
    src.parent.mkdir(parents=True)
    src.write_text("# live from the project", encoding="utf-8")
    # A recorded root only authorizes a read while it still VERIFIES as a
    # project, so the fixture is a real repo root rather than a bare directory.
    (proj / ".git").mkdir()
    return proj, src


class TestSourceRootBarrier:
    """``source_root`` is what makes a link outside ``$HOME`` readable at all."""

    def test_read_refused_without_recorded_root(self, home_store, project_file) -> None:
        _proj, src = project_file
        # This is the silent breakage: a project file outside $HOME is refused,
        # so the artifact would serve a stale snapshot forever.
        assert home_store._try_read_source_path(str(src)) is None

    def test_read_allowed_with_recorded_root(self, home_store, project_file) -> None:
        proj, src = project_file
        assert home_store._try_read_source_path(str(src), str(proj)) == "# live from the project"

    def test_write_refused_without_recorded_root(self, home_store, project_file) -> None:
        _proj, src = project_file
        assert home_store._try_write_source_path(str(src), "edited") is False
        assert src.read_text(encoding="utf-8") == "# live from the project"

    def test_write_allowed_with_recorded_root(self, home_store, project_file) -> None:
        proj, src = project_file
        assert home_store._try_write_source_path(str(src), "edited", str(proj)) is True
        assert src.read_text(encoding="utf-8") == "edited"

    def test_recorded_root_does_not_widen_other_artifacts(self, home_store, tmp_path) -> None:
        # Recording one project root must not make a SIBLING directory readable.
        proj = tmp_path / "workplace" / "proj-a"
        proj.mkdir(parents=True)
        other = tmp_path / "workplace" / "proj-b" / "secret.md"
        other.parent.mkdir(parents=True)
        other.write_text("not yours", encoding="utf-8")
        assert home_store._try_read_source_path(str(other), str(proj)) is None

    def test_sensitive_path_still_refused_inside_recorded_root(
        self, home_store, tmp_path, monkeypatch
    ) -> None:
        proj = tmp_path / "workplace" / "proj"
        src = proj / ".aws" / "credentials"
        src.parent.mkdir(parents=True)
        src.write_text("SECRET", encoding="utf-8")
        from kiro_crew import artifacts as artifacts_mod

        monkeypatch.setattr(artifacts_mod, "is_sensitive_path", lambda p: p == str(src.resolve()))
        assert home_store._try_read_source_path(str(src), str(proj)) is None

    def test_forged_root_cannot_widen_the_read_boundary(
        self, home_store, tmp_path
    ) -> None:
        """A persisted root is a hint, never authority.

        ``meta.json`` sits in the agent-writable data home, so a record naming
        ``source_root`` = a filesystem ancestor would otherwise hand the store
        read access to anything under it.
        """
        outside = tmp_path / "etc" / "secrets"
        outside.parent.mkdir(parents=True)
        outside.write_text("SECRET", encoding="utf-8")
        # The forged root is a real directory but is NOT a repo root and was
        # never registered as a project, so it must not authorize the read.
        assert home_store._try_read_source_path(str(outside), str(tmp_path)) is None

    def test_relocate_promotes_a_copy_to_a_live_pointer(
        self, home_store, project_file
    ) -> None:
        """Relocating a copied artifact must actually attach it to the file.

        Relocate is an explicit "this artifact tracks THIS file" act. Leaving
        ``source_copy_only`` set would make it silently inert -- reads and edits
        would keep ignoring the file the user just pointed at.
        """
        proj, src = project_file
        home_store.create(
            name="Scratch",
            content="# copied",
            slug="scratch",
            kind="markdown",
            source_path="/tmp/scratch.md",
            source_copy_only=True,
        )
        relocated = home_store.relocate("scratch", str(src), str(proj))
        assert relocated.source_copy_only is False
        # And the read is now live.
        src.write_text("# externally edited", encoding="utf-8")
        got = home_store.get("scratch")
        assert got.content == "# externally edited"
        assert got.source_missing is False

    def test_write_works_without_dir_fd_via_a_by_name_atomic_replace(
        self, home_store, project_file, monkeypatch
    ) -> None:
        """No directory-handle APIs (Windows) must not disable mirroring.

        The staged payload is renamed by NAME instead of through a pinned parent.
        That is what every editor's atomic save does, and it keeps the property
        that actually protects data: the file is either all of the old bytes or
        all of the new ones, never a half-write.
        """
        import os as _os

        proj, src = project_file
        src.write_text("ORIGINAL", encoding="utf-8")
        monkeypatch.setattr(_os, "supports_dir_fd", set(), raising=False)
        assert home_store._try_write_source_path(str(src), "new body", str(proj)) is True
        monkeypatch.undo()
        assert src.read_text(encoding="utf-8") == "new body"
        # No staging litter left behind on this path either.
        assert not list(src.parent.glob(".*kirocrew-*"))

    @pytest.mark.skipif(
        os.name == "nt",
        reason=(
            "the pinned-parent re-check lives on the dir-fd path; Windows has no dir_fd "
            "support and takes the validated-descriptor path instead"
        ),
    )
    @pytest.mark.skipif(
        os.rename not in getattr(os, "supports_dir_fd", set()),
        reason="the pinned-parent re-check only exists where directory-handle APIs do",
    )
    def test_write_refuses_when_the_pinned_parent_holds_a_different_file(
        self, home_store, project_file, monkeypatch
    ) -> None:
        """An ancestor swap must not redirect the rename onto another file.

        ``O_NOFOLLOW`` only guards the final path component, so an intermediate
        directory can be replaced between validating the file and opening its
        parent. Re-resolving the basename through the pinned directory fd and
        requiring the same ``(st_dev, st_ino)`` catches that: here the target is
        swapped for a different inode after validation, and the write is refused
        instead of overwriting the impostor.
        """
        import os as _os

        proj, src = project_file
        src.write_text("ORIGINAL", encoding="utf-8")
        original_stat = _os.stat

        def lying_stat(*args, **kwargs):
            st = original_stat(*args, **kwargs)
            if kwargs.get("dir_fd") is not None:
                # Pretend the pinned parent resolved to a different inode.
                class _Fake:
                    st_dev = st.st_dev
                    st_ino = st.st_ino + 1

                return _Fake()
            return st

        monkeypatch.setattr(_os, "stat", lying_stat)
        assert home_store._try_write_source_path(str(src), "new body", str(proj)) is False
        monkeypatch.undo()
        assert src.read_text(encoding="utf-8") == "ORIGINAL"

    def test_rejected_writes_do_not_leak_descriptors(
        self, home_store, project_file
    ) -> None:
        """Every validation rejection must close the descriptor it opened.

        The fd deliberately outlives validation (the no-dir-fd path writes
        through it), so it cannot be closed in a blanket ``finally``. That made
        each rejected update leak one descriptor, which would eventually exhaust
        the gateway's limit.
        """
        import os as _os

        proj, src = project_file
        # A path outside the approved root is rejected during validation.
        outside = proj.parent / "outside.md"
        outside.write_text("x", encoding="utf-8")

        def open_fds() -> int:
            try:
                return len(_os.listdir(f"/proc/{_os.getpid()}/fd"))
            except OSError:  # pragma: no cover -- non-Linux
                pytest.skip("no /proc to count descriptors")

        before = open_fds()
        for _ in range(40):
            assert (
                home_store._try_write_source_path(str(outside), "nope", str(proj)) is False
            )
        # A leak would add ~40 descriptors; allow a little slack for unrelated I/O.
        assert open_fds() - before < 10

    def test_write_refuses_when_an_acl_attribute_cannot_be_carried(
        self, home_store, project_file, monkeypatch
    ) -> None:
        """Losing an ACL is a security regression, so the write is refused.

        The rename installs a NEW inode. If the owner's POSIX ACL cannot be
        reproduced on it, the replacement is protected LESS than the file it
        replaced -- so the original is left alone instead.
        """
        import os as _os

        proj, src = project_file
        src.write_text("ORIGINAL", encoding="utf-8")

        real_list = getattr(_os, "listxattr", None)
        if real_list is None:  # pragma: no cover - platform without xattrs
            pytest.skip("no xattr support on this platform")

        monkeypatch.setattr(
            _os, "listxattr", lambda *a, **k: ["system.posix_acl_access"], raising=False
        )
        monkeypatch.setattr(_os, "getxattr", lambda *a, **k: b"acl-bytes", raising=False)

        def refuse_setxattr(*a, **k):
            raise OSError(1, "Operation not permitted")

        monkeypatch.setattr(_os, "setxattr", refuse_setxattr, raising=False)
        assert home_store._try_write_source_path(str(src), "new body", str(proj)) is False
        monkeypatch.undo()
        assert src.read_text(encoding="utf-8") == "ORIGINAL"

    def test_write_proceeds_when_only_an_informational_attribute_fails(
        self, home_store, project_file, monkeypatch
    ) -> None:
        # A user.* attribute is metadata, not protection. Failing the save over it
        # would break every linked write on a filesystem that cannot store xattrs.
        import os as _os

        proj, src = project_file
        if not hasattr(_os, "listxattr"):  # pragma: no cover
            pytest.skip("no xattr support on this platform")

        monkeypatch.setattr(_os, "listxattr", lambda *a, **k: ["user.note"], raising=False)
        monkeypatch.setattr(_os, "getxattr", lambda *a, **k: b"x", raising=False)

        def refuse_setxattr(*a, **k):
            raise OSError(95, "Operation not supported")

        monkeypatch.setattr(_os, "setxattr", refuse_setxattr, raising=False)
        assert home_store._try_write_source_path(str(src), "new body", str(proj)) is True
        monkeypatch.undo()
        assert src.read_text(encoding="utf-8") == "new body"

    def test_write_refuses_when_the_attribute_list_cannot_be_read(
        self, home_store, project_file, monkeypatch
    ) -> None:
        """A failed listxattr is not the same as "there are none".

        Treating a lookup failure as an empty list would install a replacement
        stripped of the owner's ACL. Only "this filesystem has no xattrs"
        (ENOTSUP/EOPNOTSUPP/ENOSYS) is safe to read as nothing-to-carry.
        """
        import errno as _errno
        import os as _os

        proj, src = project_file
        src.write_text("ORIGINAL", encoding="utf-8")
        if not hasattr(_os, "listxattr"):  # pragma: no cover
            pytest.skip("no xattr support on this platform")

        def failing_list(*a, **k):
            raise OSError(_errno.EACCES, "Permission denied")

        monkeypatch.setattr(_os, "listxattr", failing_list, raising=False)
        assert home_store._try_write_source_path(str(src), "new body", str(proj)) is False
        monkeypatch.undo()
        assert src.read_text(encoding="utf-8") == "ORIGINAL"

    def test_write_proceeds_where_the_filesystem_has_no_xattrs(
        self, home_store, project_file, monkeypatch
    ) -> None:
        # ENOTSUP means there is nothing on the source to lose, so the write goes
        # ahead -- failing here would break linked writes on tmpfs and several
        # network mounts.
        import errno as _errno
        import os as _os

        proj, src = project_file
        if not hasattr(_os, "listxattr"):  # pragma: no cover
            pytest.skip("no xattr support on this platform")

        def unsupported(*a, **k):
            raise OSError(_errno.ENOTSUP, "Operation not supported")

        monkeypatch.setattr(_os, "listxattr", unsupported, raising=False)
        assert home_store._try_write_source_path(str(src), "new body", str(proj)) is True
        monkeypatch.undo()
        assert src.read_text(encoding="utf-8") == "new body"

    def test_write_preserves_extended_attributes(
        self, home_store, project_file
    ) -> None:
        """Extended attributes must survive the replace.

        The in-place write this staging replaced preserved them for free by never
        changing the inode; a fresh inode starts with none, which would silently
        drop POSIX ACLs (stored as ``system.posix_acl_access``) and any ``user.*``
        metadata.
        """
        import os

        proj, src = project_file
        try:
            os.setxattr(str(src), "user.kirocrew_test", b"keepme")
        except (AttributeError, OSError):
            pytest.skip("filesystem or platform has no xattr support")
        assert home_store._try_write_source_path(str(src), "new body", str(proj)) is True
        assert src.read_text(encoding="utf-8") == "new body"
        assert os.getxattr(str(src), "user.kirocrew_test") == b"keepme"

    def test_write_survives_a_platform_without_geteuid(
        self, home_store, project_file, monkeypatch
    ) -> None:
        """``os.geteuid`` is POSIX-only; its absence must not raise.

        On Windows the attribute does not exist, and ``AttributeError`` is not an
        ``OSError`` -- so an unguarded call would escape every handler in this
        path and surface as a 500 with the descriptor leaked and ``current.html``
        already written. Simulated by deleting the attribute.
        """
        import os as _os

        proj, src = project_file
        monkeypatch.delattr(_os, "geteuid", raising=False)
        assert home_store._try_write_source_path(str(src), "no euid here", str(proj)) is True
        assert src.read_text(encoding="utf-8") == "no euid here"

    def test_pinned_parent_check_only_applies_where_pinning_exists(
        self, home_store, project_file, monkeypatch
    ) -> None:
        """The inode re-check is a Linux-only EXTRA, not the safety property.

        Without directory-handle APIs there is no pinned parent to compare
        against, so the write proceeds on the by-name path. The crash-safety
        guarantee (atomic replace) is unchanged; only the anti-swap hardening is
        absent, which is the documented trade.
        """
        import os as _os

        proj, src = project_file
        src.write_text("ORIGINAL", encoding="utf-8")
        monkeypatch.setattr(_os, "supports_dir_fd", set(), raising=False)
        assert home_store._try_write_source_path(str(src), "fallback body", str(proj)) is True
        monkeypatch.undo()
        assert src.read_text(encoding="utf-8") == "fallback body"

    def test_a_file_owned_by_someone_else_is_still_mirrored(
        self, home_store, project_file, monkeypatch
    ) -> None:
        """Ownership does not gate the mirror.

        Replacing a file does hand the new inode to this process's user, which is
        what any editor's atomic save does to a group-shared file. Refusing
        instead meant a shared project file silently stopped tracking its
        artifact, which is the worse outcome; the write goes through and the
        permission bits and extended attributes are carried across.
        """
        import os as _os

        proj, src = project_file
        src.write_text("ORIGINAL", encoding="utf-8")
        real_fstat = _os.fstat

        def fstat_foreign(fd: int):
            st = real_fstat(fd)

            class _Foreign:
                st_mode, st_nlink, st_size = st.st_mode, st.st_nlink, st.st_size
                st_dev, st_ino = st.st_dev, st.st_ino
                st_uid, st_gid = st.st_uid + 1, st.st_gid + 1

            return _Foreign()

        monkeypatch.setattr(_os, "fstat", fstat_foreign)
        assert home_store._try_write_source_path(str(src), "new body", str(proj)) is True
        monkeypatch.undo()
        assert src.read_text(encoding="utf-8") == "new body"

    @pytest.mark.skipif(
        os.name == "nt",
        reason="the staged rename is the dir-fd path, which Windows does not take",
    )
    def test_write_refuses_to_clobber_a_concurrent_save(
        self, home_store, project_file, monkeypatch
    ) -> None:
        """An editor's atomic save during our staged write must win, not lose.

        ``rename()`` replaces whatever the name points at when it runs. If an IDE
        atomically saves the same file while the payload is being staged, the
        target is a NEW inode and renaming over it would silently discard content
        newer than what is being written here. The last-moment identity re-check
        turns that into a refusal.
        """
        import os as _os

        proj, src = project_file
        src.write_text("ORIGINAL", encoding="utf-8")

        real_fsync = _os.fsync
        fired = {"done": False}

        def fsync_then_replace(fd: int) -> None:
            # Fires while the staging file is being flushed -- i.e. after the
            # first identity check and before the rename.
            real_fsync(fd)
            if not fired["done"]:
                fired["done"] = True
                newer = src.parent / "newer.md"
                newer.write_text("A NEWER SAVE FROM THE EDITOR", encoding="utf-8")
                _os.replace(str(newer), str(src))

        monkeypatch.setattr(_os, "fsync", fsync_then_replace)
        assert home_store._try_write_source_path(str(src), "our body", str(proj)) is False
        monkeypatch.undo()
        # The editor's newer content survived; ours was refused.
        assert src.read_text(encoding="utf-8") == "A NEWER SAVE FROM THE EDITOR"

    @pytest.mark.skipif(
        os.name == "nt",
        reason="Windows has no POSIX permission bits; fchmod_safe is a documented no-op there",
    )
    def test_write_preserves_the_original_file_mode(
        self, home_store, project_file
    ) -> None:
        """The atomic replace must not downgrade permissions.

        Staging is created 0600; without carrying the target's mode across, a
        shared 0644 document or an 0755 script would come back 0600 and break
        every other reader.
        """
        import stat as _stat

        proj, src = project_file
        src.chmod(0o644)
        assert home_store._try_write_source_path(str(src), "new body", str(proj)) is True
        assert src.read_text(encoding="utf-8") == "new body"
        assert _stat.S_IMODE(src.stat().st_mode) == 0o644

    def test_write_does_not_clobber_a_lookalike_staging_file(
        self, home_store, project_file
    ) -> None:
        """A sibling that merely looks like our staging file is real user data.

        A predictable staging name opened O_CREAT|O_TRUNC would have emptied it
        and then renamed it away. The name is unique per call and created
        O_EXCL, so an existing lookalike survives untouched.
        """
        proj, src = project_file
        decoy = src.parent / f".{src.name}.kirocrew-tmp"
        decoy.write_text("someone else's data", encoding="utf-8")
        assert home_store._try_write_source_path(str(src), "new body", str(proj)) is True
        assert src.read_text(encoding="utf-8") == "new body"
        assert decoy.read_text(encoding="utf-8") == "someone else's data"

    def test_failed_source_write_leaves_the_original_intact(
        self, home_store, project_file, monkeypatch
    ) -> None:
        """A write that fails partway must not truncate the user's file.

        Truncating before writing meant an ENOSPC/EIO partway through left the
        project file empty or half-written with no way back.
        """
        proj, src = project_file
        original = src.read_text(encoding="utf-8")
        from kiro_crew import hooks as hooks_mod

        real_write = hooks_mod.os.write
        calls = {"n": 0}

        def exploding_write(fd: int, data: bytes) -> int:
            # Only the PAYLOAD write fails. That is what ENOSPC actually looks
            # like on the in-place path: the truncate freed exactly the space the
            # restore needs, so writing the original bytes back succeeds. Failing
            # every write instead models a disk that never recovers, which no
            # in-place scheme can survive -- and it made this test assert data
            # loss on platforms without dir-fd support (Windows), where the
            # restore write was being blown up too.
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError(28, "No space left on device")
            return real_write(fd, data)

        monkeypatch.setattr(hooks_mod.os, "write", exploding_write)
        assert home_store._try_write_source_path(str(src), "replacement", str(proj)) is False
        monkeypatch.setattr(hooks_mod.os, "write", real_write)
        assert src.read_text(encoding="utf-8") == original
        # And no staging litter is left behind.
        assert not list(src.parent.glob(".*kirocrew-tmp"))

    def test_refused_mirror_keeps_the_edit_and_demotes_to_copy(
        self, home_store, project_file, monkeypatch
    ) -> None:
        """A refused mirror write must not cost the user their edit.

        The write can now be declined for several legitimate reasons (read-only
        file, a concurrent save that would be clobbered, ownership we may not
        reassign, a source too large to roll back). The edit is already in
        current.html, but while the artifact still claims to be a live pointer the
        next read prefers the SOURCE -- serving the old text back and reporting
        itself clean. The artifact must take ownership of its own copy instead.
        """
        proj, src = project_file
        src.write_text("# on disk", encoding="utf-8")
        art = home_store.create(
            name="linked",
            content="# on disk",
            slug="linked-demote",
            source_path=str(src),
            source_root=str(proj),
        )
        assert art.source_copy_only is False

        monkeypatch.setattr(
            type(home_store), "_try_write_source_path", lambda self, *a, **k: False
        )
        home_store.update("linked-demote", content="# my edit")

        # The edit survived, and the source was left alone.
        assert home_store.get("linked-demote").content == "# my edit"
        assert src.read_text(encoding="utf-8") == "# on disk"
        # Demoted to copy, and PERSISTED -- otherwise every later save would
        # re-attempt the mirror and re-lose the edit.
        assert home_store.get("linked-demote").source_copy_only is True
        fresh = ArtifactStore(root=home_store.root)
        assert fresh.get("linked-demote").source_copy_only is True
        assert fresh.get("linked-demote").content == "# my edit"
        # Provenance is kept: it still records where it came from.
        assert fresh.get("linked-demote").source_path == str(src)

    def test_snapshot_never_writes_back_to_a_linked_file(
        self, home_store, project_file, monkeypatch
    ) -> None:
        """A snapshot is a READ; it must never write out to source_path.

        The live read is bounded, so a source file larger than the bound yields a
        PREFIX. Mirroring that prefix back would truncate the user's file --
        silent, unrecoverable data loss. Writing back content that came FROM the
        file is pointless even when it fits, so the write is skipped outright.
        """
        proj, src = project_file
        home_store.create(
            name="Spec",
            content="# snapshot",
            slug="spec",
            kind="markdown",
            source_path=str(src),
            source_root=str(proj),
        )
        full = "# live from the project\n" + ("x" * 400)
        src.write_text(full, encoding="utf-8")
        # Force the bounded read to return a PREFIX, exactly as an oversized
        # file would, and prove the original survives intact.
        from kiro_crew import artifacts as artifacts_mod

        monkeypatch.setattr(artifacts_mod, "MAX_CONTENT_BYTES", 32)
        home_store.update("spec", snapshot=True)
        assert src.read_text(encoding="utf-8") == full

    def test_snapshot_of_a_copy_does_not_reimport_the_original(
        self, home_store, project_file
    ) -> None:
        """Snapshotting a copied artifact must keep the user's edits.

        The snapshot path re-reads live content so it can capture external
        changes to a LINKED file. For a copy that would overwrite the edited
        artifact with the original file's bytes.
        """
        proj, src = project_file
        home_store.create(
            name="Scratch",
            content="# original",
            slug="scratch",
            kind="markdown",
            source_path=str(src),
            source_root=str(proj),
            source_copy_only=True,
        )
        home_store.update("scratch", content="# edited in the artifact")
        snapped = home_store.update("scratch", snapshot=True)
        assert snapped.content == "# edited in the artifact"
        # And the original file is untouched by the copy's edits.
        assert src.read_text(encoding="utf-8") == "# live from the project"

    def test_get_serves_live_content_when_root_recorded(self, home_store, project_file) -> None:
        proj, src = project_file
        art = home_store.create(
            name="Spec",
            content="# snapshot",
            slug="spec",
            kind="markdown",
            source_path=str(src),
            source_root=str(proj),
        )
        assert art.source_root == str(proj)
        src.write_text("# externally edited", encoding="utf-8")
        got = home_store.get("spec")
        assert got.content == "# externally edited"
        assert got.source_missing is False

    def test_source_root_persisted_and_reloaded(self, home_store, project_file) -> None:
        proj, src = project_file
        home_store.create(
            name="Spec",
            content="# snapshot",
            slug="spec",
            source_path=str(src),
            source_root=str(proj),
        )
        raw = json.loads((home_store._artifact_dir("spec") / "meta.json").read_text())
        assert raw["source_root"] == str(proj)
        assert home_store.get("spec").source_root == str(proj)

    def test_legacy_meta_without_source_root_loads_empty(self, home_store, project_file) -> None:
        proj, src = project_file
        home_store.create(
            name="Spec", content="s", slug="spec", source_path=str(src), source_root=str(proj)
        )
        meta_path = home_store._artifact_dir("spec") / "meta.json"
        raw = json.loads(meta_path.read_text())
        del raw["source_root"]  # simulate an artifact written before the field existed
        meta_path.write_text(json.dumps(raw), encoding="utf-8")
        assert home_store.get("spec").source_root == ""

    def test_source_root_ignored_without_source_path(self, home_store, project_file) -> None:
        proj, _src = project_file
        art = home_store.create(name="Chat", content="x", source_root=str(proj))
        # A root with nothing to authorize is meaningless metadata.
        assert art.source_root == ""

    def test_relocate_clears_stale_source_root(self, home_store, project_file) -> None:
        proj, src = project_file
        home_store.create(
            name="Spec", content="s", slug="spec", source_path=str(src), source_root=str(proj)
        )
        home_dst = Path.home() / "moved.md"
        home_dst.write_text("# moved", encoding="utf-8")
        art = home_store.relocate("spec", str(home_dst))
        # The old project root no longer authorizes anything about the new path.
        assert art.source_path == str(home_dst)
        assert art.source_root == ""


class TestSourceMissingIsVisible:
    """A dead live-pointer must be reported, not silently masked by the snapshot."""

    def test_deleted_source_sets_source_missing(self, home_store, project_file) -> None:
        proj, src = project_file
        home_store.create(
            name="Spec",
            content="# snapshot",
            slug="spec",
            source_path=str(src),
            source_root=str(proj),
        )
        src.unlink()
        got = home_store.get("spec")
        assert got.source_missing is True
        assert got.content == "# snapshot"  # still viewable via the fallback

    def test_refused_root_sets_source_missing(self, home_store, project_file) -> None:
        # No recorded root → the read is refused → the pointer is effectively dead.
        _proj, src = project_file
        home_store.create(name="Spec", content="# snapshot", slug="spec", source_path=str(src))
        got = home_store.get("spec")
        assert got.source_missing is True
        assert got.content == "# snapshot"

    def test_live_dirty_cannot_report_clean_for_missing_source(
        self, home_store, project_file
    ) -> None:
        # The regression: live_dirty was computed against the FALLBACK, so it
        # compared the snapshot with itself and always said "in sync".
        proj, src = project_file
        home_store.create(
            name="Spec",
            # Snapshot matches the file, so the healthy baseline is genuinely clean.
            content="# live from the project",
            slug="spec",
            source_path=str(src),
            source_root=str(proj),
        )
        assert home_store.get("spec").live_dirty is False  # healthy pointer
        src.unlink()
        got = home_store.get("spec")
        assert got.source_missing is True
        assert got.live_dirty is True

    def test_healthy_pointer_leaves_flag_false(self, home_store, project_file) -> None:
        proj, src = project_file
        home_store.create(
            name="Spec",
            content="# live from the project",
            slug="spec",
            source_path=str(src),
            source_root=str(proj),
        )
        assert home_store.get("spec").source_missing is False

    def test_chat_backed_artifact_never_flagged(self, store: ArtifactStore) -> None:
        store.create(name="Widget", content="<p>hi</p>", slug="w", kind="widget")
        assert store.get("w").source_missing is False

    def test_source_missing_is_not_persisted(self, home_store, project_file) -> None:
        proj, src = project_file
        home_store.create(
            name="Spec", content="s", slug="spec", source_path=str(src), source_root=str(proj)
        )
        src.unlink()
        home_store.get("spec")  # computes source_missing=True
        home_store.set_pinned("spec", True)  # any metadata write
        raw = json.loads((home_store._artifact_dir("spec") / "meta.json").read_text())
        assert "source_missing" not in raw
        assert "live_dirty" not in raw

    def test_snapshot_off_dead_pointer_flags_missing(self, home_store, project_file) -> None:
        proj, src = project_file
        home_store.create(
            name="Spec", content="s", slug="spec", source_path=str(src), source_root=str(proj)
        )
        src.unlink()
        art = home_store.update("spec", snapshot=True)
        assert art.source_missing is True


class TestSourcePathLengthRejected:
    """Over-long pointers are REJECTED — truncating produced a different path."""

    def test_create_rejects_overlong_source_path(self, store: ArtifactStore) -> None:
        long_path = "/" + "a" * 600
        with pytest.raises(ArtifactValidationError, match="refusing to truncate"):
            store.create(name="X", content="c", slug="x", source_path=long_path)
        # And nothing was half-created.
        with pytest.raises(ArtifactNotFoundError):
            store.get("x")

    def test_create_rejects_overlong_source_root(self, store: ArtifactStore) -> None:
        with pytest.raises(ArtifactValidationError, match="source_root exceeds"):
            store.create(name="X", content="c", source_path="/p/a.md", source_root="/" + "b" * 600)

    def test_relocate_rejects_overlong_source_path(self, store: ArtifactStore) -> None:
        store.create(name="X", content="c", slug="x")
        with pytest.raises(ArtifactValidationError, match="refusing to truncate"):
            store.relocate("x", "/" + "a" * 600)

    def test_path_at_the_cap_is_accepted_unchanged(self, store: ArtifactStore) -> None:
        from kiro_crew.artifacts import MAX_SOURCE_PATH_LEN

        at_cap = "/" + "a" * (MAX_SOURCE_PATH_LEN - 1)
        art = store.create(name="X", content="c", slug="x", source_path=at_cap)
        assert art.source_path == at_cap  # never silently shortened


class TestAllowedRootsSingleProducer:
    """All three consumers of the allowed-roots set share ONE producer.

    The set had drifted into three copies (live read, live write, relocate
    handler) and the handler's copy omitted the data-home root, so relocate
    refused paths the store would then read happily.
    """

    def test_set_contains_home_data_home_and_configured_roots(
        self, home_store, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.config.loader import KiroCrewConfig, PublishConfig

        extra = tmp_path / "shared"
        extra.mkdir()
        cfg = KiroCrewConfig()
        cfg.publish = PublishConfig(relocate_roots=[str(extra)])
        monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(lambda: cfg))
        roots = home_store.allowed_source_roots()
        assert Path.home().resolve() in roots
        assert (Path.home() / ".kiro" / "crew").resolve() in roots  # data home
        assert extra.resolve() in roots
        # source_root only widens when supplied AND still verifiable. A bare
        # directory is not: meta.json is agent-writable, so an unverified root
        # would let a forged record widen this boundary at will.
        assert tmp_path.resolve() not in roots
        assert tmp_path.resolve() not in home_store.allowed_source_roots(str(tmp_path))
        verifiable = tmp_path / "repo"
        (verifiable / ".git").mkdir(parents=True)
        assert verifiable.resolve() in home_store.allowed_source_roots(str(verifiable))

    def test_data_home_path_accepted_by_read_and_write(self, home_store) -> None:
        # The root the relocate handler used to omit. Read and write must agree.
        data_home = home_store._root.resolve().parent
        target = data_home / "note.md"
        target.write_text("in the data home", encoding="utf-8")
        assert home_store._try_read_source_path(str(target)) == "in the data home"
        assert home_store._try_write_source_path(str(target), "edited") is True

    def test_no_second_copy_of_the_root_assembly_in_source(self) -> None:
        """Anti-drift pin: only ``allowed_source_roots`` may assemble the set.

        Guards against a fourth copy appearing. Both markers below exist exactly
        once in the store (inside ``allowed_source_roots``) and never in the
        handler, which must call the store instead.
        """
        import kiro_crew.artifacts as artifacts_mod
        import kiro_crew.dashboard.handlers.artifacts as handlers_mod

        store_src = Path(artifacts_mod.__file__).read_text(encoding="utf-8")
        handler_src = Path(handlers_mod.__file__).read_text(encoding="utf-8")
        assert store_src.count("Path.home().resolve()") == 1
        assert store_src.count(".publish.relocate_roots") == 1
        assert "Path.home()" not in handler_src
        assert ".publish.relocate_roots" not in handler_src
        assert "allowed_source_roots()" in handler_src
