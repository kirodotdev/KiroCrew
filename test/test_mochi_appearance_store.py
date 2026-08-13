"""Appearance-pack persistence.

The pack tree is what makes a user-imported avatar survive a restart, and two of
its behaviors are easy to break without noticing: preserving ``source.png`` on
overwrite (lose it and the pack can never be re-edited) and confining pack ids
and filenames to the tree (lose that and a crafted id writes outside it).
"""

from __future__ import annotations

import base64
import json
import zipfile
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.mochi.appearance_store import (
    MANIFEST_FILE,
    SOURCE_FILE,
    PackError,
    appearances_dir,
    delete_pack,
    export_pack,
    get_pack_detail,
    import_bundle,
    list_packs,
    read_pack_file,
    save_pack,
    save_sprite_pack,
)

PNG = "data:image/png;base64," + base64.b64encode(b"fake-png-bytes").decode()
SHEET = "data:image/png;base64," + base64.b64encode(b"fake-sheet-bytes").decode()


def _payload(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "name": "Test Pet",
        "author": "someone",
        "description": "a pet",
        "frameWidth": 32,
        "frameHeight": 32,
        "fps": 8,
        "assignments": {"idle": PNG, "walking": PNG},
        "sourceImage": SHEET,
    }
    base.update(over)
    return base


class TestSave:
    def test_failed_overwrite_leaves_the_existing_pack_intact(self, tmp_path: Path) -> None:
        """Every image is decoded BEFORE the old pack is touched. The overwrite
        path used to rmtree first and decode after, so one malformed data URI
        destroyed the pack it was replacing."""
        pack_id = save_sprite_pack(tmp_path, _payload())
        pack = appearances_dir(tmp_path) / pack_id

        with pytest.raises(PackError):
            save_sprite_pack(
                tmp_path,
                _payload(overwriteId=pack_id, assignments={"idle": "data:image/png;base64,%%%"}),
            )

        # The rejected overwrite must not have deleted anything.
        assert (pack / MANIFEST_FILE).is_file()
        assert (pack / "idle.png").read_bytes() == b"fake-png-bytes"
        assert (pack / "walking.png").is_file()

    def test_writes_a_manifest_and_one_png_per_slot(self, tmp_path: Path) -> None:
        pack_id = save_sprite_pack(tmp_path, _payload())
        pack = appearances_dir(tmp_path) / pack_id

        assert (pack / MANIFEST_FILE).is_file()
        assert (pack / "idle.png").read_bytes() == b"fake-png-bytes"
        assert (pack / "walking.png").is_file()

    def test_keeps_the_source_sheet_so_the_pack_stays_editable(self, tmp_path: Path) -> None:
        pack_id = save_sprite_pack(tmp_path, _payload())
        assert (
            appearances_dir(tmp_path) / pack_id / SOURCE_FILE
        ).read_bytes() == b"fake-sheet-bytes"

    def test_sorts_slots_into_states_and_moods(self, tmp_path: Path) -> None:
        # `sleeping` is a MOOD in the renderer's schema; filing it as a state put
        # it where the resolver never looks it up, so the art silently vanished.
        pack_id = save_sprite_pack(
            tmp_path, _payload(assignments={"idle": PNG, "happy": PNG, "sleeping": PNG})
        )
        manifest = get_pack_detail(tmp_path, pack_id)
        assert manifest is not None
        assert set(manifest["states"]) == {"idle"}
        assert set(manifest["moods"]) == {"happy", "sleeping"}

    def test_state_classification_mirrors_the_renderer_schema(self, tmp_path: Path) -> None:
        """Every slot the editor can offer must land in ``states``.

        These three (`offline` / `peeking` / `peekThinking`) were missing from the
        backend's list while the renderer still emitted them, so art assigned to
        them was misfiled as a mood and never rendered — with no error anywhere.
        """
        slots = {
            "idle": PNG,
            "done": PNG,
            "error": PNG,
            "loading": PNG,
            "walking": PNG,
            "thinking": PNG,
            "working": PNG,
            "offline": PNG,
            "peeking": PNG,
            "peekThinking": PNG,
        }
        pack_id = save_sprite_pack(tmp_path, _payload(assignments=slots))
        manifest = get_pack_detail(tmp_path, pack_id)
        assert manifest is not None
        assert set(manifest["states"]) == set(slots)
        assert manifest["moods"] == {}

    def test_thumbnail_points_at_idle(self, tmp_path: Path) -> None:
        pack_id = save_sprite_pack(tmp_path, _payload())
        manifest = get_pack_detail(tmp_path, pack_id)
        assert manifest is not None
        assert manifest["meta"]["thumbnail"] == "idle.png"

    def test_source_key_is_absent_not_null_when_there_is_no_sheet(self, tmp_path: Path) -> None:
        # Wire-format rule for this port: optional keys stay absent.
        pack_id = save_sprite_pack(tmp_path, _payload(sourceImage=None))
        manifest = get_pack_detail(tmp_path, pack_id)
        assert manifest is not None
        assert "source" not in manifest["sprite"]

    def test_a_pack_with_no_slots_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(PackError):
            save_sprite_pack(tmp_path, _payload(assignments={}))

    def test_malformed_image_data_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(PackError):
            save_sprite_pack(tmp_path, _payload(assignments={"idle": "not-base64!!"}))


class TestOverwrite:
    def test_overwrite_replaces_in_place_under_the_same_id(self, tmp_path: Path) -> None:
        first = save_sprite_pack(tmp_path, _payload())
        second = save_sprite_pack(tmp_path, _payload(overwriteId=first, name="Renamed"))

        assert second == first
        manifest = get_pack_detail(tmp_path, first)
        assert manifest is not None
        assert manifest["meta"]["name"] == "Renamed"

    def test_overwrite_without_a_new_sheet_preserves_the_old_one(self, tmp_path: Path) -> None:
        # The load-bearing one: the editor re-slices source.png, so an overwrite
        # that dropped it would silently make the pack un-editable.
        pack_id = save_sprite_pack(tmp_path, _payload())
        save_sprite_pack(tmp_path, _payload(overwriteId=pack_id, sourceImage=None))

        kept = appearances_dir(tmp_path) / pack_id / SOURCE_FILE
        assert kept.read_bytes() == b"fake-sheet-bytes"

    def test_overwrite_drops_slots_that_are_no_longer_assigned(self, tmp_path: Path) -> None:
        pack_id = save_sprite_pack(tmp_path, _payload(assignments={"idle": PNG, "walking": PNG}))
        save_sprite_pack(tmp_path, _payload(overwriteId=pack_id, assignments={"idle": PNG}))

        pack = appearances_dir(tmp_path) / pack_id
        assert (pack / "idle.png").is_file()
        assert not (pack / "walking.png").exists()

    def test_a_write_failure_mid_overwrite_leaves_the_old_pack_intact(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The replacement is staged, so a failed save cannot destroy user data.

        Decoding up front already stopped a malformed data URI from deleting the
        pack it replaced, but the WRITES can still fail — a full disk halfway
        through used to leave the old pack removed and the new one incomplete, and
        a pack the user built by hand was simply gone.
        """
        pack_id = save_sprite_pack(tmp_path, _payload(name="Original"))
        pack = appearances_dir(tmp_path) / pack_id
        before = sorted(p.name for p in pack.iterdir())

        real_write_bytes = Path.write_bytes
        calls = {"n": 0}

        def _explode(self: Path, data: bytes) -> int:
            calls["n"] += 1
            if calls["n"] == 2:  # first slot write after the source sheet
                raise OSError("No space left on device")
            return real_write_bytes(self, data)

        monkeypatch.setattr(Path, "write_bytes", _explode)
        with pytest.raises(OSError):
            save_sprite_pack(tmp_path, _payload(overwriteId=pack_id, name="Replacement"))
        monkeypatch.undo()

        # Old pack still there, unchanged, and no staging debris left behind.
        assert sorted(p.name for p in pack.iterdir()) == before
        detail = get_pack_detail(tmp_path, pack_id)
        assert detail is not None and detail["meta"]["name"] == "Original"
        assert [p.name for p in appearances_dir(tmp_path).iterdir() if ".staging-" in p.name] == []
        assert [p.name for p in appearances_dir(tmp_path).iterdir() if ".old-" in p.name] == []


class TestStagedOverwriteAcrossEveryWriter:
    """All three pack writers stage before replacing, not just the sprite path.

    The first fix here covered `save_sprite_pack` only, so a `.mochipack.zip`
    import and the editor's per-slot save could still half-overwrite a live pack.
    These pin the shared `_staged_pack` swap for the other two.
    """

    def test_a_corrupt_bundle_entry_leaves_the_existing_pack_intact(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        pack_id = save_sprite_pack(tmp_path, _payload(name="Keeper"))
        pack = appearances_dir(tmp_path) / pack_id
        before = sorted(p.name for p in pack.iterdir())
        blob = export_pack(tmp_path, pack_id)

        real_read = zipfile.ZipFile.read

        def _boom(self, name):  # noqa: ANN001 - test double
            info = name if isinstance(name, str) else name.filename
            if info.endswith(".png"):
                raise zipfile.BadZipFile("CRC failed")
            return real_read(self, name)

        monkeypatch.setattr(zipfile.ZipFile, "read", _boom)
        with pytest.raises(Exception):
            import_bundle(tmp_path, blob)
        monkeypatch.undo()

        assert sorted(p.name for p in pack.iterdir()) == before
        detail = get_pack_detail(tmp_path, pack_id)
        assert detail is not None and detail["meta"]["name"] == "Keeper"
        assert [p.name for p in appearances_dir(tmp_path).iterdir() if ".staging-" in p.name] == []

    def test_a_failed_editor_save_leaves_the_existing_pack_intact(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        meta = {"id": "edit-me", "name": "Original", "format": "svg"}
        save_pack(tmp_path, meta, {"idle": "<svg>a</svg>"})
        pack = appearances_dir(tmp_path) / "edit-me"
        before = sorted(p.name for p in pack.iterdir())

        import kiro_crew.apps.builtins.mochi.appearance_store as store

        real = store.atomic_write
        calls = {"n": 0}

        def _explode(path, content, **kw):  # noqa: ANN001 - test double
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("No space left on device")
            return real(path, content, **kw)

        monkeypatch.setattr(store, "atomic_write", _explode)
        with pytest.raises(OSError):
            save_pack(tmp_path, {**meta, "name": "Replacement"}, {"idle": "<svg>b</svg>"})
        monkeypatch.undo()

        assert sorted(p.name for p in pack.iterdir()) == before
        detail = get_pack_detail(tmp_path, "edit-me")
        assert detail is not None and detail["meta"]["name"] == "Original"


class TestListing:
    def test_empty_when_nothing_saved(self, tmp_path: Path) -> None:
        assert list_packs(tmp_path) == []

    def test_lists_saved_packs(self, tmp_path: Path) -> None:
        save_sprite_pack(tmp_path, _payload(name="A"))
        save_sprite_pack(tmp_path, _payload(name="B"))
        assert {p["name"] for p in list_packs(tmp_path)} == {"A", "B"}

    def test_a_corrupt_pack_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        # One bad directory must not hide every other pack.
        good = save_sprite_pack(tmp_path, _payload(name="Good"))
        broken = appearances_dir(tmp_path) / "broken-pack"
        broken.mkdir(parents=True)
        (broken / MANIFEST_FILE).write_text("{not json")

        names = [p["name"] for p in list_packs(tmp_path)]
        assert names == ["Good"]
        assert get_pack_detail(tmp_path, good) is not None

    def test_detail_of_an_unknown_pack_is_none(self, tmp_path: Path) -> None:
        assert get_pack_detail(tmp_path, "nope") is None


class TestPathConfinement:
    """A pack id and a filename both come from the client."""

    @pytest.mark.parametrize("bad", ["../escape", "a/b", "", ".hidden", "x" * 100])
    def test_unsafe_pack_ids_are_rejected(self, tmp_path: Path, bad: str) -> None:
        with pytest.raises(PackError):
            get_pack_detail(tmp_path, bad)

    @pytest.mark.parametrize("bad", ["../../etc/passwd", "sub/dir.png", ".env", ""])
    def test_unsafe_filenames_are_rejected(self, tmp_path: Path, bad: str) -> None:
        pack_id = save_sprite_pack(tmp_path, _payload())
        with pytest.raises(PackError):
            read_pack_file(tmp_path, pack_id, bad)

    def test_a_legitimate_file_reads_back(self, tmp_path: Path) -> None:
        pack_id = save_sprite_pack(tmp_path, _payload())
        assert read_pack_file(tmp_path, pack_id, "idle.png") == b"fake-png-bytes"

    def test_a_missing_file_is_none_not_an_error(self, tmp_path: Path) -> None:
        pack_id = save_sprite_pack(tmp_path, _payload())
        assert read_pack_file(tmp_path, pack_id, "absent.png") is None


class TestDelete:
    def test_removes_the_pack(self, tmp_path: Path) -> None:
        pack_id = save_sprite_pack(tmp_path, _payload())
        assert delete_pack(tmp_path, pack_id) is True
        assert not (appearances_dir(tmp_path) / pack_id).exists()

    def test_deleting_an_unknown_pack_is_false_not_an_error(self, tmp_path: Path) -> None:
        assert delete_pack(tmp_path, "never-existed") is False

    def test_manifest_is_valid_json_on_disk(self, tmp_path: Path) -> None:
        pack_id = save_sprite_pack(tmp_path, _payload())
        raw = (appearances_dir(tmp_path) / pack_id / MANIFEST_FILE).read_text()
        assert json.loads(raw)["meta"]["id"] == pack_id


class TestBundleRoundTrip:
    """`.mochipack.zip` export/import — the format is upstream's and must stay
    compatible, but the EXTRACTION is ours and the bundle is untrusted input."""

    @staticmethod
    def _bundle(entries: dict[str, bytes]) -> bytes:
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name, data in entries.items():
                z.writestr(name, data)
        return buf.getvalue()

    @staticmethod
    def _manifest(pack_id: str = "imported") -> bytes:
        return json.dumps(
            {
                "meta": {
                    "id": pack_id,
                    "name": "Imported",
                    "author": "a",
                    "description": "d",
                    "type": "built-in",
                    "format": "svg",
                    "thumbnail": "idle.svg",
                },
                "states": {"idle": "idle.svg"},
                "moods": {},
            }
        ).encode()

    def test_import_then_export_round_trips(self, tmp_path) -> None:
        from kiro_crew.apps.builtins.mochi.appearance_store import export_pack, import_bundle

        blob = self._bundle({"manifest.json": self._manifest(), "idle.svg": b"<svg/>"})
        meta = import_bundle(tmp_path, blob)
        assert meta["id"] == "imported"
        # An imported pack is user-owned whatever the bundle claimed, or a user
        # could not delete what they just imported.
        assert meta["type"] == "custom"
        again = export_pack(tmp_path, "imported")
        import io
        import zipfile

        with zipfile.ZipFile(io.BytesIO(again)) as z:
            assert sorted(z.namelist()) == ["idle.svg", "manifest.json"]

    def test_path_traversal_entry_is_refused(self, tmp_path) -> None:
        from kiro_crew.apps.builtins.mochi.appearance_store import PackError, import_bundle

        # zipfile.extractall would happily write through this.
        blob = self._bundle({"manifest.json": self._manifest(), "../escaped.svg": b"<svg/>"})
        with pytest.raises(PackError, match="Unsafe entry"):
            import_bundle(tmp_path, blob)
        assert not (tmp_path.parent / "escaped.svg").exists()

    def test_nested_directory_entry_is_refused(self, tmp_path) -> None:
        from kiro_crew.apps.builtins.mochi.appearance_store import PackError, import_bundle

        blob = self._bundle({"manifest.json": self._manifest(), "sub/idle.svg": b"<svg/>"})
        with pytest.raises(PackError, match="Unsafe entry"):
            import_bundle(tmp_path, blob)

    def test_disallowed_extension_is_refused(self, tmp_path) -> None:
        from kiro_crew.apps.builtins.mochi.appearance_store import PackError, import_bundle

        blob = self._bundle({"manifest.json": self._manifest(), "run.sh": b"#!/bin/sh\n"})
        with pytest.raises(PackError, match="Disallowed file"):
            import_bundle(tmp_path, blob)

    def test_zip_bomb_is_refused_before_writing(self, tmp_path) -> None:
        from kiro_crew.apps.builtins.mochi.appearance_store import (
            MAX_BUNDLE_BYTES,
            PackError,
            import_bundle,
        )

        big = b"a" * (MAX_BUNDLE_BYTES + 1)
        blob = self._bundle({"manifest.json": self._manifest(), "big.svg": big})
        with pytest.raises(PackError, match="too large"):
            import_bundle(tmp_path, blob)

    def test_missing_manifest_is_refused(self, tmp_path) -> None:
        from kiro_crew.apps.builtins.mochi.appearance_store import PackError, import_bundle

        with pytest.raises(PackError, match="Missing manifest"):
            import_bundle(tmp_path, self._bundle({"idle.svg": b"<svg/>"}))

    def test_corrupt_archive_is_refused(self, tmp_path) -> None:
        from kiro_crew.apps.builtins.mochi.appearance_store import PackError, import_bundle

        with pytest.raises(PackError, match="corrupted"):
            import_bundle(tmp_path, b"not a zip")

    def test_export_of_a_missing_pack_raises(self, tmp_path) -> None:
        from kiro_crew.apps.builtins.mochi.appearance_store import PackError, export_pack

        with pytest.raises(PackError, match="not found"):
            export_pack(tmp_path, "nope")

    def test_export_reports_a_referenced_file_that_is_gone(self, tmp_path) -> None:
        from kiro_crew.apps.builtins.mochi.appearance_store import (
            PackError,
            export_pack,
            import_bundle,
        )

        import_bundle(tmp_path, self._bundle({"manifest.json": self._manifest(), "idle.svg": b"x"}))
        (tmp_path / "appearances" / "imported" / "idle.svg").unlink()
        # Upstream verified existence BEFORE writing, so a half-built bundle never
        # reaches the user.
        with pytest.raises(PackError, match="Missing file for export"):
            export_pack(tmp_path, "imported")


class TestSavePackFromContent:
    """The pack editor's save path (upstream AppearanceRegistry.savePack)."""

    def test_a_traversal_mood_slot_is_refused(self, tmp_path) -> None:
        """The slot becomes a FILENAME, and mood slots arrive verbatim from the
        request body (the route only checks ``isinstance(moods, dict)``). Without
        the allow-list a mood named ``../../x`` escapes the pack directory and
        ``atomic_write`` — which mkdir -p's the resolved parent — plants the
        content anywhere the gateway can write, the governance keystone
        included. State slots were always safe (fixed tuples)."""
        from kiro_crew.apps.builtins.mochi.appearance_store import PackError, save_pack

        with pytest.raises(PackError, match="invalid slot name"):
            save_pack(
                tmp_path,
                {"id": "t", "format": "lottie"},
                {"idle": '{"v":"5"}'},
                {"../../../../escaped": '{"v":"5"}'},
            )
        # Nothing was planted outside the appearances tree.
        assert not (tmp_path.parent / "escaped.json").exists()

    def test_identical_content_is_written_once_and_shared(self, tmp_path) -> None:
        from kiro_crew.apps.builtins.mochi.appearance_store import get_pack_detail, save_pack

        same = "<svg id='a'/>"
        meta = save_pack(
            tmp_path,
            {"id": "p", "name": "P", "format": "svg"},
            {"idle": same, "walking": same, "thinking": "<svg id='b'/>"},
        )
        detail = get_pack_detail(tmp_path, meta["id"])
        assert detail is not None
        # Upstream deduplicated by content: an author reusing one drawing for
        # several slots must not multiply the files (and the export size).
        assert detail["states"]["idle"] == detail["states"]["walking"]
        assert detail["states"]["thinking"] != detail["states"]["idle"]
        files = sorted(f.name for f in (tmp_path / "appearances" / "p").iterdir())
        assert files == ["idle.svg", "manifest.json", "thinking.svg"]

    def test_extension_follows_the_declared_format(self, tmp_path) -> None:
        from kiro_crew.apps.builtins.mochi.appearance_store import save_pack

        save_pack(tmp_path, {"id": "l", "format": "lottie"}, {"idle": '{"v":"5"}'})
        # The renderer chooses its loader from the extension, so a Lottie written
        # as .svg renders nothing.
        assert (tmp_path / "appearances" / "l" / "idle.json").is_file()

    def test_missing_idle_is_refused(self, tmp_path) -> None:
        from kiro_crew.apps.builtins.mochi.appearance_store import PackError, save_pack

        with pytest.raises(PackError, match="Missing required states"):
            save_pack(tmp_path, {"id": "x"}, {"walking": "<svg/>"})

    def test_only_idle_is_required(self, tmp_path) -> None:
        from kiro_crew.apps.builtins.mochi.appearance_store import save_pack

        # Divergence from upstream, kept deliberately: it demanded all six states
        # while having almost no fallbacks, so a one-drawing pack rendered five
        # states blank.
        assert save_pack(tmp_path, {"id": "one"}, {"idle": "<svg/>"})["id"] == "one"

    def test_an_editor_pack_is_always_user_owned(self, tmp_path) -> None:
        from kiro_crew.apps.builtins.mochi.appearance_store import save_pack

        meta = save_pack(tmp_path, {"id": "b", "type": "built-in"}, {"idle": "<svg/>"})
        # Otherwise the user could not delete what they just created.
        assert meta["type"] == "custom"

    def test_resaving_prunes_art_for_removed_slots(self, tmp_path) -> None:
        from kiro_crew.apps.builtins.mochi.appearance_store import save_pack

        save_pack(tmp_path, {"id": "p"}, {"idle": "<svg id='1'/>", "error": "<svg id='2'/>"})
        save_pack(tmp_path, {"id": "p"}, {"idle": "<svg id='1'/>"})
        files = sorted(f.name for f in (tmp_path / "appearances" / "p").iterdir())
        assert files == ["idle.svg", "manifest.json"]


class TestImportValidatesBeforeReplacing:
    """A bundle reusing an existing pack's id must be validated BEFORE staging.
    `_staged_pack` swaps atomically, so an invalid bundle (missing the required
    `idle` state, or a manifest referencing a file the archive lacks) would
    irreversibly replace a good, hand-built pack with an unusable one.
    """

    def _bundle(self, manifest: dict, members: dict) -> bytes:
        import io
        import json as _json
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(MANIFEST_FILE, _json.dumps(manifest))
            for name, data in members.items():
                z.writestr(name, data)
        return buf.getvalue()

    def test_missing_idle_state_is_refused_and_keeps_the_old_pack(self, tmp_path: Path) -> None:
        pack_id = save_sprite_pack(tmp_path, _payload(name="Keeper"))
        # A bundle reusing that id but with no `idle` in states.
        blob = self._bundle(
            {"meta": {"id": pack_id, "name": "Evil"}, "states": {}, "moods": {}},
            {},
        )
        with pytest.raises(PackError, match="required state"):
            import_bundle(tmp_path, blob)
        detail = get_pack_detail(tmp_path, pack_id)
        assert detail is not None and detail["meta"]["name"] == "Keeper", "old pack must survive"

    def test_manifest_referencing_a_missing_file_is_refused(self, tmp_path: Path) -> None:
        pack_id = save_sprite_pack(tmp_path, _payload(name="Keeper"))
        blob = self._bundle(
            {"meta": {"id": pack_id, "name": "Evil"}, "states": {"idle": "idle.png"}, "moods": {}},
            {},  # idle.png referenced but NOT in the archive
        )
        with pytest.raises(PackError, match="missing file"):
            import_bundle(tmp_path, blob)
        detail = get_pack_detail(tmp_path, pack_id)
        assert detail is not None and detail["meta"]["name"] == "Keeper"


class TestActivityLogDoesNotBlockTheLoop:
    def test_log_activity_holds_a_file_lock(self) -> None:
        import inspect

        from kiro_crew.apps.builtins.mochi import activity_log

        # log_activity locks the read-modify-write via the shared
        # activity_mutation contextmanager (also used by the reset unlink), which
        # in turn acquires a cross-process file lock.
        src = inspect.getsource(activity_log.log_activity)
        assert (
            "activity_mutation(" in src
        ), "the read-modify-write must be locked for concurrent writers"
        lock_src = inspect.getsource(activity_log.activity_mutation)
        assert "file_lock(" in lock_src, "activity_mutation must acquire a cross-process file lock"

    def test_hooks_offloads_the_write_when_on_the_loop(self) -> None:
        import inspect

        from kiro_crew.apps.builtins.mochi import hooks

        src = inspect.getsource(hooks.MochiRuntime._log_activity)
        assert "run_in_executor(" in src and "get_running_loop()" in src
