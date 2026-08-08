"""The avatar library.

Weighted towards the two things that are unrecoverable if they go wrong: a custom
pack the user made by hand, and a path that escapes the packs directory. A pack is
third-party content — possibly hand-edited JSON — so the read path has to survive
anything without taking the companion down with it.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.apps.builtins.crew_companion.appearances import (
    DEFAULT_PACK,
    AppearanceStore,
)


def _write_pack(root, pack_id, *, fmt="svg", name=None, states=None):
    """Put a minimally valid custom pack on disk."""
    d = root / "appearances" / pack_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "idle.svg").write_text("<svg/>", "utf-8")
    manifest = {
        "meta": {
            "id": pack_id,
            "name": name or pack_id,
            "author": "someone",
            "description": "a test character",
            "type": "custom",
            "format": fmt,
        },
        "states": states if states is not None else {"idle": "idle.svg"},
    }
    (d / "manifest.json").write_text(json.dumps(manifest), "utf-8")
    return d


@pytest.fixture()
def store(tmp_path):
    s = AppearanceStore(tmp_path)
    s.load()
    return s


class TestListing:
    def test_the_built_in_ghost_is_always_offered(self, store):
        # It ships with the app rather than living on disk, so an empty library still
        # has to produce it — otherwise a fresh install has no companion at all.
        ids = [p["id"] for p in store.list_packs()]
        assert ids == [DEFAULT_PACK]

    def test_the_built_in_ghost_cannot_be_deleted(self, store):
        assert store.delete_pack(DEFAULT_PACK) is False
        assert DEFAULT_PACK in [p["id"] for p in store.list_packs()]

    def test_custom_packs_appear_after_the_built_in(self, tmp_path, store):
        _write_pack(tmp_path, "cat")
        ids = [p["id"] for p in store.list_packs()]
        assert ids[0] == DEFAULT_PACK
        assert "cat" in ids

    def test_one_broken_pack_does_not_hide_the_others(self, tmp_path, store):
        _write_pack(tmp_path, "good")
        broken = tmp_path / "appearances" / "broken"
        broken.mkdir(parents=True)
        (broken / "manifest.json").write_text("{ not json", "utf-8")

        ids = [p["id"] for p in store.list_packs()]
        # The whole point: a hand-edited manifest costs the user ONE pack, not the
        # entire library.
        assert "good" in ids
        assert "broken" not in ids

    def test_a_directory_with_no_manifest_is_skipped(self, tmp_path, store):
        (tmp_path / "appearances" / "stray").mkdir(parents=True)
        assert "stray" not in [p["id"] for p in store.list_packs()]


class TestDetail:
    def test_animation_content_is_inlined(self, tmp_path, store):
        _write_pack(tmp_path, "cat")
        detail = store.pack_detail("cat")
        assert detail is not None
        assert detail["animations"]["idle"]["content"] == "<svg/>"

    def test_an_unknown_pack_returns_none(self, store):
        assert store.pack_detail("nope") is None

    def test_a_state_naming_a_missing_file_is_skipped_not_fatal(self, tmp_path, store):
        _write_pack(tmp_path, "cat", states={"idle": "idle.svg", "done": "gone.svg"})
        detail = store.pack_detail("cat")
        # The pack still renders with what it does have; a manifest referencing art
        # the user deleted should degrade, not fail.
        assert "idle" in detail["animations"]
        assert "done" not in detail["animations"]


class TestPathSafety:
    """A pack id becomes a directory name, so it is a path boundary."""

    @pytest.mark.parametrize(
        "bad",
        ["../escape", "..", ".", "a/b", "a\\b", "", "   ", "x" * 65, "with.dot"],
    )
    def test_a_traversal_or_separator_is_refused(self, store, bad):
        assert store.pack_detail(bad) is None
        assert store.delete_pack(bad) is False
        assert store.save_pack(bad, {"meta": {}}, {}) is False

    def test_a_traversing_filename_in_a_manifest_reads_nothing(self, tmp_path, store):
        # The manifest is user-editable, so a filename is as much a boundary as an id.
        _write_pack(tmp_path, "cat", states={"idle": "../../../etc/passwd"})
        detail = store.pack_detail("cat")
        assert detail["animations"] == {}

    def test_deleting_a_pack_removes_only_that_pack(self, tmp_path, store):
        _write_pack(tmp_path, "keep")
        _write_pack(tmp_path, "drop")
        assert store.delete_pack("drop") is True
        ids = [p["id"] for p in store.list_packs()]
        assert "keep" in ids
        assert "drop" not in ids

    def test_a_spoofed_manifest_id_cannot_impersonate_another_pack(self, tmp_path, store):
        """The DIRECTORY is the identity. Listing used to trust the manifest's
        self-declared meta.id, so a hand-edited pack could list itself under
        ANOTHER pack's id — and deleting/selecting that gallery entry hit the
        victim's directory. Same identity-spoof class as the import-path fix."""
        _write_pack(tmp_path, "victim")
        _write_pack(tmp_path, "impostor")
        manifest_path = tmp_path / "appearances" / "impostor" / "manifest.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest["meta"]["id"] = "victim"
        manifest_path.write_text(json.dumps(manifest), "utf-8")

        ids = sorted(p["id"] for p in store.list_packs() if p["id"] != DEFAULT_PACK)
        assert ids == ["impostor", "victim"]
        # Deleting the impostor touches only the impostor's directory.
        assert store.delete_pack("impostor") is True
        assert (tmp_path / "appearances" / "victim").is_dir()

    def test_detail_carries_the_stored_source_sheet(self, tmp_path, store):
        """The ORIGINAL sheet is kept for re-editing but is not an animation
        slot, so it never appeared in `animations` — and the editor's lookup
        through the slot map came back empty. The editor then fell back to
        strips and an overwrite save permanently DROPPED the sheet. The detail
        payload now carries it as `sourceImage`."""
        manifest = {
            "meta": {"id": "sheeted", "name": "S", "format": "sprite"},
            "states": {"idle": "idle.png"},
            "sprite": {"frameWidth": 32, "frameHeight": 32, "source": "source.png"},
        }
        files = {"idle.png": "c3RyaXA=", "source.png": "c2hlZXQ="}
        assert store.save_pack("sheeted", manifest, files) is True
        detail = store.pack_detail("sheeted")
        assert detail["sourceImage"] == "c2hlZXQ="
        # A pack with no stored sheet simply omits the field.
        _write_pack(tmp_path, "plain")
        assert "sourceImage" not in store.pack_detail("plain")


class TestSaving:
    def test_a_saved_pack_is_listable_and_readable(self, tmp_path, store):
        manifest = {
            "meta": {
                "id": "robot",
                "name": "Robot",
                "author": "me",
                "description": "a pixel robot",
                "type": "custom",
                "format": "svg",
            },
            "states": {"idle": "idle.svg"},
        }
        assert store.save_pack("robot", manifest, {"idle.svg": "<svg id='r'/>"}) is True
        assert "robot" in [p["id"] for p in store.list_packs()]
        assert store.pack_detail("robot")["animations"]["idle"]["content"] == "<svg id='r'/>"

    def test_replacing_a_pack_does_not_leave_the_old_art_behind(self, tmp_path, store):
        m = {"meta": {"id": "p", "format": "svg", "type": "custom"}, "states": {"idle": "a.svg"}}
        store.save_pack("p", m, {"a.svg": "<svg>one</svg>", "old.svg": "<svg>stale</svg>"})
        m2 = {"meta": {"id": "p", "format": "svg", "type": "custom"}, "states": {"idle": "a.svg"}}
        store.save_pack("p", m2, {"a.svg": "<svg>two</svg>"})

        # The replace is atomic, so the previous version's files are gone rather than
        # mingling with the new ones.
        assert (tmp_path / "appearances" / "p" / "old.svg").exists() is False
        assert store.pack_detail("p")["animations"]["idle"]["content"] == "<svg>two</svg>"

    def test_the_built_in_id_cannot_be_overwritten(self, store):
        assert store.save_pack(DEFAULT_PACK, {"meta": {}}, {}) is False

    def test_no_staging_directory_is_left_behind(self, tmp_path, store):
        m = {"meta": {"id": "p", "format": "svg", "type": "custom"}, "states": {}}
        store.save_pack("p", m, {})
        leftovers = [d.name for d in (tmp_path / "appearances").iterdir() if d.name.startswith(".tmp-")]
        assert leftovers == []

    def test_a_pack_file_named_manifest_json_is_refused(self, tmp_path, store):
        """The manifest is generated from the validated payload; a pack file
        with the reserved name overwrote it in staging, letting bundle content
        forge the manifest (and defeating the import path's inner-id
        normalization). Case-insensitive, all-or-nothing."""
        m = {"meta": {"id": "forged", "name": "F"}, "states": {"idle": "idle.svg"}}
        files = {"idle.svg": "<svg/>", "manifest.json": '{"meta": {"id": "victim"}}'}
        assert store.save_pack("forged", m, files) is False
        assert not (tmp_path / "appearances" / "forged").exists()
        # Case variant would collide on case-insensitive filesystems.
        files_upper = {"idle.svg": "<svg/>", "MANIFEST.JSON": "{}"}
        assert store.save_pack("forged", m, files_upper) is False


class TestColours:
    def test_a_recolouring_round_trips(self, tmp_path):
        s = AppearanceStore(tmp_path)
        s.load()
        assert s.set_colour_map(DEFAULT_PACK, {"#fff": "#f0f"}) is True

        # A fresh store over the same directory is what a gateway restart looks like.
        again = AppearanceStore(tmp_path)
        again.load()
        assert again.colour_map(DEFAULT_PACK) == {"#fff": "#f0f"}

    def test_a_recoloured_pack_is_flagged_in_the_listing(self, tmp_path):
        s = AppearanceStore(tmp_path)
        s.load()
        s.set_colour_map(DEFAULT_PACK, {"#fff": "#0ff"})
        builtin = next(p for p in s.list_packs() if p["id"] == DEFAULT_PACK)
        assert builtin["recoloured"] is True

    def test_non_string_colour_entries_are_dropped(self, store):
        store.set_colour_map(DEFAULT_PACK, {"#fff": 123, "#000": "#111"})
        # The renderer does a string rewrite over the SVG source, so a non-string
        # would throw there instead of here.
        assert store.colour_map(DEFAULT_PACK) == {"#000": "#111"}

    def test_a_corrupt_colour_file_does_not_stop_loading(self, tmp_path):
        (tmp_path / "crew-companion-colours.json").write_text("{ broken", "utf-8")
        s = AppearanceStore(tmp_path)
        s.load()  # must not raise
        assert s.colour_map(DEFAULT_PACK) == {}

    def test_deleting_a_pack_forgets_its_colours(self, tmp_path, store):
        _write_pack(tmp_path, "cat")
        store.set_colour_map("cat", {"#a": "#b"})
        store.delete_pack("cat")
        # Otherwise a new pack reusing the id would inherit a stranger's palette.
        assert store.colour_map("cat") == {}

    def test_a_failed_colour_write_rolls_back_and_raises(self, tmp_path, monkeypatch):
        """A disk-full recolour used to be acknowledged as SUCCESS: the write
        failure was swallowed, the route returned 200, the UI showed the new
        colour -- and a restart silently reloaded the old map. The failure must
        surface (route wrapper maps OSError to 503) and memory must roll back
        so the UI never shows a colour that disk doesn't have."""
        s = AppearanceStore(tmp_path)
        s.load()
        assert s.set_colour_map(DEFAULT_PACK, {"#fff": "#f0f"}) is True

        def boom(self_):
            raise OSError("disk full")

        monkeypatch.setattr(AppearanceStore, "_save_colours", boom)
        with pytest.raises(OSError):
            s.set_colour_map(DEFAULT_PACK, {"#fff": "#0ff"})
        # Rolled back to the last persisted map, not the failed one.
        assert s.colour_map(DEFAULT_PACK) == {"#fff": "#f0f"}

    def test_a_failed_colour_write_with_no_prior_map_rolls_back_to_empty(
        self, tmp_path, monkeypatch
    ):
        s = AppearanceStore(tmp_path)
        s.load()

        def boom(self_):
            raise OSError("disk full")

        monkeypatch.setattr(AppearanceStore, "_save_colours", boom)
        with pytest.raises(OSError):
            s.set_colour_map(DEFAULT_PACK, {"#fff": "#0ff"})
        assert s.colour_map(DEFAULT_PACK) == {}

    def test_delete_still_succeeds_when_the_colour_write_fails(
        self, tmp_path, store, monkeypatch
    ):
        """delete_pack's rmtree already happened by the time colours persist;
        failing the whole delete over a stale-but-harmless colour entry would
        report a pack as present that is actually gone."""
        _write_pack(tmp_path, "dog")
        store.set_colour_map("dog", {"#a": "#b"})

        def boom(self_):
            raise OSError("disk full")

        monkeypatch.setattr(AppearanceStore, "_save_colours", boom)
        assert store.delete_pack("dog") is True
        assert all(p["id"] != "dog" for p in store.list_packs())
